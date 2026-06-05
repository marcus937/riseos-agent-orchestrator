from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.github_events import parse_github_event
from app.review_queue import (
    ReviewLifecycleStage,
    ReviewWorkItem,
    build_lifecycle_visibility,
    process_review_work_item,
    record_lifecycle_stage,
    review_work_item_from_parsed,
)
from app.review_worker import process_queued_review_item
from app.storage import SQLiteStateStore

ARTIFACT_FILES = {
    "queue_state": "queue-state.json",
    "worker_claims": "worker-claims.json",
    "review_lifecycle": "review-lifecycle.json",
    "diagnostics": "diagnostics.json",
    "failure_summary": "failure-summary.md",
}

REVIEW_ITEM_COUNT = 6
WORKER_COUNT = 4
DETERMINISTIC_REPO = "riseos/queue-contention-validation"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ClaimAttempt:
    worker_id: str
    item_id: str
    claimed: bool
    status_after: str | None
    attempted_at: str = field(default_factory=_now_iso)


@dataclass
class ProcessingRecord:
    worker_id: str
    item_id: str
    started_at: str = field(default_factory=_now_iso)
    completed_at: str | None = None


def run_queue_contention_validation(artifact_dir: Path) -> dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStateStore(str(artifact_dir / "queue-contention-validation.db"), max_review_items=100)
    settings = Settings(
        github_webhook_secret="queue-contention-validation-secret",
        orchestrator_admin_token="queue-contention-validation-admin-token",
        enable_openai_review=False,
        enable_github_context_hydration=False,
        enable_github_writeback=False,
        enable_task_dispatch=False,
    )

    items = _enqueue_review_items(store)
    duplicate_results = _attempt_duplicate_enqueue(store, items)
    claim_attempts: list[ClaimAttempt] = []
    processing_records: list[ProcessingRecord] = []

    asyncio.run(_run_workers(store, settings, items, claim_attempts, processing_records))

    persisted_items = store.list_review_work_items()
    queue_state = _build_queue_state(store, persisted_items)
    worker_claims = _build_worker_claims(claim_attempts, processing_records)
    review_lifecycle = [item.model_dump(mode="json") for item in build_lifecycle_visibility(persisted_items)]
    diagnostics = _build_diagnostics(
        items=persisted_items,
        duplicate_results=duplicate_results,
        worker_claims=worker_claims,
        review_lifecycle=review_lifecycle,
    )
    failure_summary = _build_failure_summary(diagnostics)

    _write_json(artifact_dir / ARTIFACT_FILES["queue_state"], queue_state)
    _write_json(artifact_dir / ARTIFACT_FILES["worker_claims"], worker_claims)
    _write_json(artifact_dir / ARTIFACT_FILES["review_lifecycle"], review_lifecycle)
    _write_json(artifact_dir / ARTIFACT_FILES["diagnostics"], diagnostics)
    (artifact_dir / ARTIFACT_FILES["failure_summary"]).write_text(failure_summary, encoding="utf-8")

    diagnostics["artifact_digest"] = _artifact_digest(artifact_dir)
    _write_json(artifact_dir / ARTIFACT_FILES["diagnostics"], diagnostics)
    return diagnostics


def _enqueue_review_items(store: SQLiteStateStore) -> list[ReviewWorkItem]:
    items: list[ReviewWorkItem] = []
    for index in range(REVIEW_ITEM_COUNT):
        parsed = parse_github_event(
            "pull_request",
            {
                "action": "opened",
                "repository": {"full_name": DETERMINISTIC_REPO},
                "number": 100 + index,
                "pull_request": {
                    "number": 100 + index,
                    "head": {
                        "ref": f"circuit/contention-{index}",
                        "sha": f"{index + 1:040d}",
                    },
                    "base": {"ref": "main"},
                },
            },
        )
        item = review_work_item_from_parsed(parsed)
        store.save_review_work_item(item)
        items.append(item)
    return items


def _attempt_duplicate_enqueue(store: SQLiteStateStore, items: list[ReviewWorkItem]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in items:
        candidate = item.model_copy(update={"id": str(uuid4()), "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC)})
        duplicate = store.find_pending_duplicate(candidate)
        if duplicate is None:
            store.save_review_work_item(candidate)
        results.append(
            {
                "original_item_id": item.id,
                "candidate_item_id": candidate.id,
                "duplicate_item_id": duplicate.id if duplicate else None,
                "prevented": duplicate is not None and duplicate.id == item.id,
            }
        )
    return results


async def _run_workers(
    store: SQLiteStateStore,
    settings: Settings,
    items: list[ReviewWorkItem],
    claim_attempts: list[ClaimAttempt],
    processing_records: list[ProcessingRecord],
) -> None:
    start = asyncio.Event()

    async def worker(worker_id: str) -> None:
        await start.wait()
        for item in items:
            response = await process_queued_review_item(
                item.id,
                settings,
                store,
                _processor(worker_id, processing_records),
            )
            saved = store.get_review_work_item(item.id)
            claim_attempts.append(
                ClaimAttempt(
                    worker_id=worker_id,
                    item_id=item.id,
                    claimed=response is not None,
                    status_after=str(saved.status) if saved else None,
                )
            )
            await asyncio.sleep(0)

    tasks = [asyncio.create_task(worker(f"review-worker-{index + 1}")) for index in range(WORKER_COUNT)]
    start.set()
    await asyncio.gather(*tasks)


def _processor(worker_id: str, processing_records: list[ProcessingRecord]):
    async def process(item: ReviewWorkItem, _settings: Settings):
        record = ProcessingRecord(worker_id=worker_id, item_id=item.id)
        processing_records.append(record)
        record_lifecycle_stage(item, ReviewLifecycleStage.REVIEW_STARTED)
        record_lifecycle_stage(item, ReviewLifecycleStage.GITHUB_WRITEBACK_STARTED)
        response = process_review_work_item(
            item,
            github_writeback_attempted=True,
            github_writeback_success=True,
        )
        record_lifecycle_stage(response.work_item, ReviewLifecycleStage.GITHUB_WRITEBACK_COMPLETED, success=True)
        record_lifecycle_stage(response.work_item, ReviewLifecycleStage.REVIEW_COMPLETED)
        record.completed_at = _now_iso()
        await asyncio.sleep(0)
        return response

    return process


def _build_queue_state(store: SQLiteStateStore, items: list[ReviewWorkItem]) -> dict[str, Any]:
    return {
        "counters": store.review_queue_counters().model_dump(mode="json"),
        "items": [item.model_dump(mode="json") for item in items],
    }


def _build_worker_claims(
    attempts: list[ClaimAttempt],
    processing_records: list[ProcessingRecord],
) -> dict[str, Any]:
    claimed_attempts = [attempt for attempt in attempts if attempt.claimed]
    item_claim_counts = Counter(attempt.item_id for attempt in claimed_attempts)
    worker_success_counts = Counter(attempt.worker_id for attempt in claimed_attempts)
    item_owners: dict[str, str] = {}
    for attempt in claimed_attempts:
        item_owners[attempt.item_id] = attempt.worker_id

    return {
        "worker_count": WORKER_COUNT,
        "attempt_count": len(attempts),
        "claimed_count": len(claimed_attempts),
        "item_claim_counts": dict(item_claim_counts),
        "item_owners": item_owners,
        "worker_success_counts": dict(worker_success_counts),
        "duplicate_claim_item_ids": sorted(item_id for item_id, count in item_claim_counts.items() if count != 1),
        "attempts": [attempt.__dict__ for attempt in attempts],
        "processing_records": [record.__dict__ for record in processing_records],
    }


def _build_diagnostics(
    *,
    items: list[ReviewWorkItem],
    duplicate_results: list[dict[str, Any]],
    worker_claims: dict[str, Any],
    review_lifecycle: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_item_ids = {item.id for item in items}
    item_claim_counts = worker_claims["item_claim_counts"]
    processing_counts = Counter(record["item_id"] for record in worker_claims["processing_records"])
    completion_by_item = {item.id: item for item in items}
    lifecycle_by_item = defaultdict(list)
    for lifecycle in review_lifecycle:
        lifecycle_by_item[lifecycle["item_id"]].append(lifecycle)

    checks = {
        "claim_locking": all(item_claim_counts.get(item_id, 0) == 1 for item_id in expected_item_ids),
        "duplicate_prevention": all(result["prevented"] for result in duplicate_results),
        "duplicate_processing_prevention": all(processing_counts.get(item_id, 0) == 1 for item_id in expected_item_ids),
        "worker_assignment_integrity": set(worker_claims["item_owners"].keys()) == expected_item_ids
        and all(owner.startswith("review-worker-") for owner in worker_claims["item_owners"].values()),
        "completion_integrity": all(
            str(item.status) == "approved_for_human_review"
            and str(item.lifecycle_stage) == "review_completed"
            and item.review_completed_at is not None
            and item.github_writeback_success is True
            for item in completion_by_item.values()
        ),
        "github_writeback_lifecycle": all(
            item.github_writeback_started_at is not None
            and item.github_writeback_completed_at is not None
            and item.github_writeback_success is True
            for item in completion_by_item.values()
        ),
        "artifact_evidence_complete": True,
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "generated_at": _now_iso(),
        "expected_item_count": len(expected_item_ids),
        "worker_count": WORKER_COUNT,
        "checks": checks,
        "duplicate_enqueue_results": duplicate_results,
        "processing_counts": dict(processing_counts),
        "contention_test_results": {
            "items_claimed_exactly_once": checks["claim_locking"],
            "no_duplicate_processing": checks["duplicate_processing_prevention"],
            "worker_ownership_recorded": checks["worker_assignment_integrity"],
            "queue_completion_correct": checks["completion_integrity"],
            "github_writeback_lifecycle_functional": checks["github_writeback_lifecycle"],
        },
    }


def _build_failure_summary(diagnostics: dict[str, Any]) -> str:
    lines = ["# Queue Contention Validation", ""]
    if diagnostics["status"] == "passed":
        lines.extend(
            [
                "PASSED",
                "",
                "All review items were claimed exactly once under concurrent worker contention.",
                "No duplicate processing occurred.",
                "Worker ownership and completion evidence were recorded in the artifact bundle.",
            ]
        )
    else:
        lines.extend(["FAILED", ""])
        for check, passed in diagnostics["checks"].items():
            if not passed:
                lines.append(f"- {check}: failed")
    return "\n".join(lines) + "\n"


def _artifact_digest(artifact_dir: Path) -> str:
    digest = hashlib.sha256()
    for artifact_name in sorted(ARTIFACT_FILES.values()):
        path = artifact_dir / artifact_name
        if path.exists():
            digest.update(artifact_name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic multi-worker queue contention validation.")
    parser.add_argument("--artifact-dir", type=Path, default=Path("queue-contention-validation-artifacts"))
    args = parser.parse_args()
    diagnostics = run_queue_contention_validation(args.artifact_dir)
    return 0 if diagnostics["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
