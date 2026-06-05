from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.github_events import GitHubEventType
from app.github_writeback import writeback_review_decision
from app.review_queue import (
    ReviewLifecycleStage,
    ReviewWorkItem,
    build_lifecycle_visibility,
    build_queue_stats,
    build_worker_stats,
    process_review_work_item,
    record_lifecycle_stage,
)
from app.storage import SQLiteStateStore


DETERMINISTIC_REPO = "riseos/multi-worker-queue-contention"
DEFAULT_WORKER_COUNT = 4
DEFAULT_ITEM_COUNT = 8


@dataclass(frozen=True)
class QueueContentionValidationResult:
    passed: bool
    failures: list[str]
    artifacts: dict[str, str]
    diagnostics: dict[str, Any]


class RecordingWritebackClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.records: list[dict[str, Any]] = []

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        with self._lock:
            record_id = len(self.records) + 1
            self.records.append(
                {
                    "id": record_id,
                    "operation": "post_issue_comment",
                    "repo_full_name": repo_full_name,
                    "issue_number": issue_number,
                    "body_contains_decision": "## Review Decision" in body,
                }
            )
        return {"id": record_id}

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        with self._lock:
            record_id = len(self.records) + 1
            self.records.append(
                {
                    "id": record_id,
                    "operation": "apply_label",
                    "repo_full_name": repo_full_name,
                    "issue_number": issue_number,
                    "label": label,
                }
            )
        return {"labels": [label]}


def run_validation(
    artifacts_dir: Path,
    *,
    worker_count: int = DEFAULT_WORKER_COUNT,
    item_count: int = DEFAULT_ITEM_COUNT,
) -> QueueContentionValidationResult:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="queue-contention-") as temp_dir:
        store = SQLiteStateStore(str(Path(temp_dir) / "queue-contention.db"), max_review_items=item_count + 10)
        item_ids = seed_review_items(store, item_count=item_count)
        writeback_client = RecordingWritebackClient()
        claim_records: list[dict[str, Any]] = []
        attempt_records: list[dict[str, Any]] = []
        record_lock = threading.Lock()
        start_barrier = threading.Barrier(worker_count)

        workers = [
            threading.Thread(
                target=_worker_loop,
                args=(
                    f"worker-{index + 1}",
                    item_ids,
                    store,
                    writeback_client,
                    claim_records,
                    attempt_records,
                    record_lock,
                    start_barrier,
                ),
                name=f"queue-contention-worker-{index + 1}",
            )
            for index in range(worker_count)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=30)

        final_items = store.list_review_work_items()
        diagnostics = build_diagnostics(
            final_items,
            writeback_client.records,
            claim_records,
            attempt_records,
            worker_count=worker_count,
            item_count=item_count,
            live_workers=[worker.name for worker in workers if worker.is_alive()],
        )
        failures = validate_diagnostics(diagnostics)
        artifacts = write_artifacts(
            artifacts_dir,
            final_items,
            writeback_client.records,
            claim_records,
            attempt_records,
            diagnostics,
            failures,
        )
        return QueueContentionValidationResult(
            passed=not failures,
            failures=failures,
            artifacts=artifacts,
            diagnostics=diagnostics,
        )


def seed_review_items(store: SQLiteStateStore, *, item_count: int) -> list[str]:
    item_ids: list[str] = []
    now = datetime.now(UTC)
    for index in range(item_count):
        item = ReviewWorkItem(
            id=f"contention-item-{index + 1}",
            created_at=now,
            updated_at=now,
            repo_full_name=DETERMINISTIC_REPO,
            event_type=GitHubEventType.PULL_REQUEST,
            branch=f"circuit/contention-{index + 1}",
            commit_sha=f"{index + 1:040x}",
            pr_number=700 + index,
        )
        store.save_review_work_item(item)
        item_ids.append(item.id)
    return item_ids


def _worker_loop(
    worker_id: str,
    item_ids: list[str],
    store: SQLiteStateStore,
    writeback_client: RecordingWritebackClient,
    claim_records: list[dict[str, Any]],
    attempt_records: list[dict[str, Any]],
    record_lock: threading.Lock,
    start_barrier: threading.Barrier,
) -> None:
    start_barrier.wait(timeout=10)
    for item_id in item_ids:
        claimed = store.claim_review_work_item(item_id)
        with record_lock:
            attempt_records.append({"worker_id": worker_id, "item_id": item_id, "claimed": claimed is not None})
        if claimed is None:
            continue

        claimed_at = claimed.worker_claimed_at.isoformat() if claimed.worker_claimed_at else None
        with record_lock:
            claim_records.append({"worker_id": worker_id, "item_id": item_id, "claimed_at": claimed_at})

        record_lifecycle_stage(claimed, ReviewLifecycleStage.REVIEW_STARTED)
        response = process_review_work_item(
            claimed,
            changed_files=["app/review_queue.py", "app/storage.py"],
            diff_summary="Deterministic multi-worker queue contention validation.",
        )
        record_lifecycle_stage(response.work_item, ReviewLifecycleStage.GITHUB_WRITEBACK_STARTED)
        writeback = asyncio.run(writeback_review_decision(response, writeback_client))
        response.github_writeback_attempted = writeback.attempted
        response.github_writeback_success = writeback.success
        response.github_writeback_error = writeback.error
        record_lifecycle_stage(
            response.work_item,
            ReviewLifecycleStage.GITHUB_WRITEBACK_COMPLETED,
            success=writeback.success,
            error=writeback.error,
        )
        record_lifecycle_stage(response.work_item, ReviewLifecycleStage.REVIEW_COMPLETED)
        store.save_review_work_item(response.work_item)


def build_diagnostics(
    final_items: list[ReviewWorkItem],
    writeback_records: list[dict[str, Any]],
    claim_records: list[dict[str, Any]],
    attempt_records: list[dict[str, Any]],
    *,
    worker_count: int,
    item_count: int,
    live_workers: list[str],
) -> dict[str, Any]:
    claims_by_item: dict[str, list[str]] = {}
    for record in claim_records:
        claims_by_item.setdefault(str(record["item_id"]), []).append(str(record["worker_id"]))
    duplicate_claim_items = {item_id: workers for item_id, workers in claims_by_item.items() if len(workers) > 1}
    completed_items = [item for item in final_items if item.review_completed_at is not None]
    writeback_pairs = {
        int(record["issue_number"])
        for record in writeback_records
        if record.get("operation") == "post_issue_comment"
    } & {
        int(record["issue_number"])
        for record in writeback_records
        if record.get("operation") == "apply_label"
    }
    return {
        "worker_count": worker_count,
        "item_count": item_count,
        "attempt_count": len(attempt_records),
        "claim_count": len(claim_records),
        "unique_claimed_item_count": len(claims_by_item),
        "duplicate_claim_items": duplicate_claim_items,
        "completed_count": len(completed_items),
        "writeback_record_count": len(writeback_records),
        "writeback_completed_item_count": len(writeback_pairs),
        "live_workers_after_join": live_workers,
        "queue_stats": build_queue_stats(final_items).model_dump(mode="json"),
        "worker_stats": build_worker_stats(final_items, auto_processing_enabled=True).model_dump(mode="json"),
    }


def validate_diagnostics(diagnostics: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    item_count = int(diagnostics["item_count"])
    if diagnostics["live_workers_after_join"]:
        failures.append(f"Workers did not finish: {diagnostics['live_workers_after_join']}")
    if diagnostics["claim_count"] != item_count:
        failures.append(f"Expected {item_count} claims, found {diagnostics['claim_count']}.")
    if diagnostics["unique_claimed_item_count"] != item_count:
        failures.append(f"Expected {item_count} uniquely claimed items, found {diagnostics['unique_claimed_item_count']}.")
    if diagnostics["duplicate_claim_items"]:
        failures.append(f"Duplicate claims detected: {diagnostics['duplicate_claim_items']}")
    if diagnostics["completed_count"] != item_count:
        failures.append(f"Expected {item_count} completed items, found {diagnostics['completed_count']}.")
    if diagnostics["writeback_completed_item_count"] != item_count:
        failures.append(f"Expected {item_count} writeback completions, found {diagnostics['writeback_completed_item_count']}.")
    queue_counters = diagnostics["queue_stats"]["counters"]
    if queue_counters["pending_review_count"] != 0 or queue_counters["reviewing_count"] != 0:
        failures.append(f"Queue still has unfinished items: {queue_counters}")
    return failures


def write_artifacts(
    artifacts_dir: Path,
    final_items: list[ReviewWorkItem],
    writeback_records: list[dict[str, Any]],
    claim_records: list[dict[str, Any]],
    attempt_records: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    failures: list[str],
) -> dict[str, str]:
    queue_state = {
        "items": [item.model_dump(mode="json") for item in final_items],
        "stats": diagnostics["queue_stats"],
    }
    worker_claims = {
        "claims": claim_records,
        "attempts": attempt_records,
        "duplicate_claim_items": diagnostics["duplicate_claim_items"],
    }
    review_lifecycle = {
        "lifecycle": [item.model_dump(mode="json") for item in build_lifecycle_visibility(final_items)],
        "github_writeback_records": writeback_records,
    }
    artifacts = {
        "queue-state.json": artifacts_dir / "queue-state.json",
        "worker-claims.json": artifacts_dir / "worker-claims.json",
        "review-lifecycle.json": artifacts_dir / "review-lifecycle.json",
        "diagnostics.json": artifacts_dir / "diagnostics.json",
        "failure-summary.md": artifacts_dir / "failure-summary.md",
    }
    _write_json(artifacts["queue-state.json"], queue_state)
    _write_json(artifacts["worker-claims.json"], worker_claims)
    _write_json(artifacts["review-lifecycle.json"], review_lifecycle)
    _write_json(artifacts["diagnostics.json"], diagnostics)
    _write_failure_summary(artifacts["failure-summary.md"], failures, diagnostics)
    return {name: str(path) for name, path in artifacts.items()}


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _write_failure_summary(path: Path, failures: list[str], diagnostics: dict[str, Any]) -> None:
    if failures:
        lines = ["# Multi-Worker Queue Contention Validation", "", "FAILED", ""]
        lines.extend(f"- {failure}" for failure in failures)
    else:
        lines = [
            "# Multi-Worker Queue Contention Validation",
            "",
            "PASSED",
            "",
            "- Every review item was claimed exactly once.",
            "- No duplicate processing occurred.",
            "- Worker ownership was recorded in worker-claims.json.",
            "- Queue completion state has no pending or reviewing items.",
            "- GitHub writeback comment and label lifecycle completed for every item.",
        ]
    lines.extend(
        ["", "## Diagnostics", f"- worker_count: {diagnostics['worker_count']}", f"- item_count: {diagnostics['item_count']}"]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic multi-worker queue contention validation.")
    parser.add_argument("--artifacts-dir", required=True, type=Path)
    parser.add_argument("--worker-count", type=int, default=DEFAULT_WORKER_COUNT)
    parser.add_argument("--item-count", type=int, default=DEFAULT_ITEM_COUNT)
    args = parser.parse_args()
    result = run_validation(args.artifacts_dir, worker_count=args.worker_count, item_count=args.item_count)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
