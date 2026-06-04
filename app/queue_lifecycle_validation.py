import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.github_events import GitHubEventType
from app.review_queue import (
    ReviewLifecycleStage,
    ReviewProcessResponse,
    ReviewWorkItem,
    ReviewWorkItemStatus,
    build_recent_failures,
    process_review_work_item,
    record_lifecycle_stage,
)
from app.review_worker import process_queued_review_item
from app.storage import SQLiteStateStore

REQUIRED_LIFECYCLE_EVENTS = [
    "review_work_item",
    "worker_claimed",
    "review_started",
    "review_completed",
    "github_writeback_started",
    "github_writeback_completed",
]

ARTIFACT_FILES = {
    "timeline": "lifecycle-timeline.json",
    "state_transitions": "state-transition-log.json",
    "correlation_tracking": "correlation-tracking.json",
    "failure_diagnostics": "failure-diagnostics.json",
    "summary_json": "validation-summary.json",
    "summary_markdown": "validation-summary.md",
}


def run_queue_lifecycle_validation(artifact_dir: str | Path) -> dict[str, Any]:
    artifact_path = Path(artifact_dir)
    artifact_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteStateStore(str(artifact_path / "queue-lifecycle-validation.db"), max_review_items=50)
    settings = Settings(github_webhook_secret="ci-webhook-secret", orchestrator_admin_token="ci-admin-token")

    success_item = _review_item(pr_number=3101, commit_sha="1" * 40)
    store.save_review_work_item(success_item)
    success_events = asyncio.run(_process_success_path(success_item.id, settings, store))
    success_saved = _require_item(store, success_item.id)

    retry_item = _review_item(pr_number=3102, commit_sha="2" * 40)
    store.save_review_work_item(retry_item)
    failure_events = asyncio.run(_process_failure_path(retry_item.id, settings, store))
    failed_saved = _require_item(store, retry_item.id)
    failure_snapshot = _failure_snapshot(store)
    retry_events = asyncio.run(_process_success_path(retry_item.id, settings, store, retry=True))
    retry_saved = _require_item(store, retry_item.id)

    timeline = success_events + failure_events + retry_events
    state_transitions = [_state_transition(event) for event in timeline]
    correlation_tracking = _correlation_tracking(timeline, success_saved, retry_saved)
    failure_diagnostics = {
        "failure_persisted_before_retry": failure_snapshot,
        "retry_after_failure": {
            "item_id": retry_saved.id,
            "status": retry_saved.status.value,
            "lifecycle_stage": retry_saved.lifecycle_stage.value,
            "failure_count_preserved": retry_saved.failure_count,
            "last_error_preserved": retry_saved.last_error,
            "github_writeback_success": retry_saved.github_writeback_success,
        },
    }

    checks = _validation_checks(timeline, correlation_tracking, failure_diagnostics, success_saved, retry_saved)
    summary = {
        "status": "passed" if all(check["passed"] for check in checks) else "failed",
        "generated_at": datetime.now(UTC).isoformat(),
        "required_lifecycle_events": REQUIRED_LIFECYCLE_EVENTS,
        "artifact_files": ARTIFACT_FILES,
        "checks": checks,
        "production_mutations": False,
        "real_github_writeback": False,
        "secret_changes": False,
    }

    _write_json(artifact_path / ARTIFACT_FILES["timeline"], timeline)
    _write_json(artifact_path / ARTIFACT_FILES["state_transitions"], state_transitions)
    _write_json(artifact_path / ARTIFACT_FILES["correlation_tracking"], correlation_tracking)
    _write_json(artifact_path / ARTIFACT_FILES["failure_diagnostics"], failure_diagnostics)
    _write_json(artifact_path / ARTIFACT_FILES["summary_json"], summary)
    _write_markdown_summary(artifact_path / ARTIFACT_FILES["summary_markdown"], summary)
    return summary


async def _process_success_path(
    item_id: str,
    settings: Settings,
    store: SQLiteStateStore,
    *,
    retry: bool = False,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    item = _require_item(store, item_id)
    events.append(_event("review_work_item", item, 1, note="review work item persisted"))

    async def processor(claimed: ReviewWorkItem, _settings: Settings) -> ReviewProcessResponse:
        events.append(_event("worker_claimed", claimed, 2, note="worker claimed pending item"))
        record_lifecycle_stage(claimed, ReviewLifecycleStage.REVIEW_STARTED)
        events.append(_event("review_started", claimed, 3, note="review processing started"))
        response = process_review_work_item(claimed)
        record_lifecycle_stage(response.work_item, ReviewLifecycleStage.REVIEW_COMPLETED)
        events.append(_event("review_completed", response.work_item, 4, note="review decision completed"))
        record_lifecycle_stage(response.work_item, ReviewLifecycleStage.GITHUB_WRITEBACK_STARTED)
        events.append(_event("github_writeback_started", response.work_item, 5, note="mock writeback started; no external write"))
        record_lifecycle_stage(response.work_item, ReviewLifecycleStage.GITHUB_WRITEBACK_COMPLETED, success=True)
        events.append(_event("github_writeback_completed", response.work_item, 6, note="mock writeback completed; no external write"))
        response.github_writeback_attempted = True
        response.github_writeback_success = True
        return response

    response = await process_queued_review_item(item_id, settings, store, processor)
    if response is None:
        raise RuntimeError("Expected success path to process a queued review item.")
    if retry:
        for event in events:
            event["retry"] = True
    return events


async def _process_failure_path(item_id: str, settings: Settings, store: SQLiteStateStore) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    item = _require_item(store, item_id)
    events.append(_event("review_work_item", item, 1, note="retry fixture work item persisted"))

    async def processor(claimed: ReviewWorkItem, _settings: Settings) -> ReviewProcessResponse:
        events.append(_event("worker_claimed", claimed, 2, note="worker claimed item before controlled failure"))
        record_lifecycle_stage(claimed, ReviewLifecycleStage.REVIEW_STARTED)
        events.append(_event("review_started", claimed, 3, note="review processing started before controlled failure"))
        raise RuntimeError("controlled lifecycle validation failure")

    response = await process_queued_review_item(item_id, settings, store, processor)
    if response is not None:
        raise RuntimeError("Expected controlled failure path to return no response.")
    failed = _require_item(store, item_id)
    events.append(_event("review_failed", failed, 4, note="failure diagnostics persisted and item reset for retry"))
    return events


def _review_item(*, pr_number: int, commit_sha: str) -> ReviewWorkItem:
    now = datetime.now(UTC)
    return ReviewWorkItem(
        id=str(uuid4()),
        created_at=now,
        updated_at=now,
        repo_full_name="marcus937/riseos-agent-orchestrator",
        event_type=GitHubEventType.PULL_REQUEST,
        branch="circuit/queue-lifecycle-validation",
        commit_sha=commit_sha,
        pr_number=pr_number,
    )


def _event(label: str, item: ReviewWorkItem, sequence_number: int, *, note: str) -> dict[str, Any]:
    return {
        "sequence_number": sequence_number,
        "event": label,
        "item_id": item.id,
        "correlation_id": _correlation_id(item),
        "repo_full_name": item.repo_full_name,
        "pr_number": item.pr_number,
        "commit_sha": item.commit_sha,
        "status": item.status.value,
        "lifecycle_stage": item.lifecycle_stage.value,
        "timestamp": _timestamp_for(label, item),
        "note": note,
    }


def _timestamp_for(label: str, item: ReviewWorkItem) -> str:
    timestamp_map = {
        "review_work_item": item.created_at,
        "worker_claimed": item.worker_claimed_at,
        "review_started": item.review_started_at,
        "review_completed": item.review_completed_at,
        "github_writeback_started": item.github_writeback_started_at,
        "github_writeback_completed": item.github_writeback_completed_at,
        "review_failed": item.last_failure_at,
    }
    return (timestamp_map.get(label) or item.updated_at or item.created_at).isoformat()


def _correlation_id(item: ReviewWorkItem) -> str:
    subject = item.pr_number if item.pr_number is not None else item.issue_number
    return f"{item.repo_full_name}:{item.event_type.value}:{subject}:{item.commit_sha}"


def _state_transition(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "sequence_number": event["sequence_number"],
        "event": event["event"],
        "item_id": event["item_id"],
        "correlation_id": event["correlation_id"],
        "status": event["status"],
        "lifecycle_stage": event["lifecycle_stage"],
        "timestamp": event["timestamp"],
    }


def _correlation_tracking(
    timeline: list[dict[str, Any]],
    success_item: ReviewWorkItem,
    retry_item: ReviewWorkItem,
) -> dict[str, Any]:
    grouped: dict[str, list[str]] = {}
    for event in timeline:
        grouped.setdefault(event["correlation_id"], []).append(event["event"])
    return {
        "correlation_ids": sorted(grouped),
        "events_by_correlation_id": grouped,
        "success_path_correlation_id": _correlation_id(success_item),
        "retry_path_correlation_id": _correlation_id(retry_item),
        "all_events_have_correlation_id": all(bool(event["correlation_id"]) for event in timeline),
    }


def _failure_snapshot(store: SQLiteStateStore) -> dict[str, Any]:
    failures = build_recent_failures(store.list_review_work_items())
    if not failures:
        raise RuntimeError("Expected a persisted recent failure diagnostic.")
    failure = failures[0]
    return failure.model_dump(mode="json")


def _validation_checks(
    timeline: list[dict[str, Any]],
    correlation_tracking: dict[str, Any],
    failure_diagnostics: dict[str, Any],
    success_item: ReviewWorkItem,
    retry_item: ReviewWorkItem,
) -> list[dict[str, Any]]:
    success_events = [event["event"] for event in timeline if event["item_id"] == success_item.id]
    retry_events = [event for event in timeline if event["item_id"] == retry_item.id]
    retry_success_events = [event["event"] for event in retry_events if event.get("retry")]
    failure_snapshot = failure_diagnostics["failure_persisted_before_retry"]
    retry_snapshot = failure_diagnostics["retry_after_failure"]
    return [
        _check("success_path_required_events", success_events == REQUIRED_LIFECYCLE_EVENTS, success_events),
        _check("success_path_ordering", _ordered(success_events, REQUIRED_LIFECYCLE_EVENTS), success_events),
        _check("correlation_id_propagation", correlation_tracking["all_events_have_correlation_id"], correlation_tracking),
        _check(
            "failure_diagnostics_persistence",
            failure_snapshot["last_error"] == "controlled lifecycle validation failure" and failure_snapshot["failure_count"] == 1,
            failure_snapshot,
        ),
        _check("retry_behavior", retry_success_events == REQUIRED_LIFECYCLE_EVENTS, retry_success_events),
        _check(
            "retry_preserves_failure_diagnostics",
            retry_snapshot["status"] == ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW.value
            and retry_snapshot["failure_count_preserved"] == 1
            and retry_snapshot["last_error_preserved"] == "controlled lifecycle validation failure",
            retry_snapshot,
        ),
    ]


def _ordered(events: list[str], expected: list[str]) -> bool:
    positions = [events.index(event) for event in expected if event in events]
    return len(positions) == len(expected) and positions == sorted(positions)


def _check(name: str, passed: bool, evidence: Any) -> dict[str, Any]:
    return {"name": name, "passed": passed, "evidence": evidence}


def _require_item(store: SQLiteStateStore, item_id: str) -> ReviewWorkItem:
    item = store.get_review_work_item(item_id)
    if item is None:
        raise RuntimeError(f"Missing review work item: {item_id}")
    return item


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_markdown_summary(path: Path, summary: dict[str, Any]) -> None:
    checks = "\n".join(
        f"- {'PASS' if check['passed'] else 'FAIL'} {check['name']}" for check in summary["checks"]
    )
    path.write_text(
        "# Queue Lifecycle Validation\n\n"
        f"Status: {summary['status']}\n\n"
        "## Safety\n\n"
        "- Production mutations: false\n"
        "- Real GitHub writeback: false\n"
        "- Secret changes: false\n\n"
        "## Checks\n\n"
        f"{checks}\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic queue lifecycle validation artifacts.")
    parser.add_argument("--artifact-dir", default="queue-lifecycle-validation-artifacts")
    args = parser.parse_args()
    summary = run_queue_lifecycle_validation(args.artifact_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
