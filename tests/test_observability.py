import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.config import Settings
from app.event_store import event_record_from_parsed
from app.github_events import GitHubEventType, parse_github_event
from app.main import app
from app.review_queue import (
    InMemoryReviewQueue,
    ReviewLifecycleStage,
    ReviewWorkItem,
    ReviewWorkItemStatus,
    build_lifecycle_visibility,
    build_queue_stats,
    build_recent_failures,
    build_worker_stats,
    process_review_work_item,
    record_lifecycle_stage,
)
from app.review_worker import process_queued_review_item
from app.review_workflow import build_review_workflow
from app.storage import SQLiteStateStore


def _item(item_id: str = "item-1") -> ReviewWorkItem:
    return ReviewWorkItem(
        id=item_id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        repo_full_name="marcus937/riseos-agent-orchestrator",
        event_type=GitHubEventType.PULL_REQUEST,
        pr_number=24,
    )


def test_queue_creation_records_lifecycle_and_correlation_key() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
            "pull_request": {
                "number": 17,
                "head": {"ref": "agent-integration", "sha": "abc123"},
                "base": {"ref": "main"},
            },
        },
    )
    workflow = build_review_workflow(parsed)
    queue = InMemoryReviewQueue()

    item = queue.create_from_review_event(parsed, workflow)
    event_record = event_record_from_parsed(parsed)

    assert item is not None
    assert item.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert item.lifecycle_stage == ReviewLifecycleStage.REVIEW_QUEUED
    assert item.repo_full_name == "marcus937/riseos-agent-orchestrator"
    assert item.branch == "agent-integration"
    assert item.commit_sha == "abc123"
    assert item.pr_number == 17
    assert event_record.correlation_key == "marcus937/riseos-agent-orchestrator:pr:17"


def test_lifecycle_stage_records_failure_text() -> None:
    item = _item()

    record_lifecycle_stage(item, ReviewLifecycleStage.REVIEW_FAILED, error="OpenAI timeout")

    assert item.lifecycle_stage == ReviewLifecycleStage.REVIEW_FAILED
    assert item.failure_count == 1
    assert item.last_error == "OpenAI timeout"
    assert item.last_failure_at is not None


def test_worker_claim_persists_transition_and_worker_statistics(tmp_path) -> None:
    store = SQLiteStateStore(str(tmp_path / "orchestrator.db"))
    item = _item("worker-claim")
    store.save_review_work_item(item)

    async def processor(work_item: ReviewWorkItem, settings: Settings):
        response = process_review_work_item(work_item)
        record_lifecycle_stage(response.work_item, ReviewLifecycleStage.REVIEW_COMPLETED)
        return response

    response = asyncio.run(process_queued_review_item(item.id, Settings(), store, processor))
    reloaded = store.get_review_work_item(item.id)
    worker_stats = build_worker_stats(store.list_review_work_items(), auto_processing_enabled=True)

    assert response is not None
    assert reloaded is not None
    assert reloaded.status == ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW
    assert reloaded.lifecycle_stage == ReviewLifecycleStage.REVIEW_COMPLETED
    assert reloaded.worker_claimed_at is not None
    assert reloaded.review_completed_at is not None
    assert worker_stats.claimed_count == 1
    assert worker_stats.completed_count == 1


def test_worker_failure_persists_exception_and_recent_diagnostics(tmp_path) -> None:
    store = SQLiteStateStore(str(tmp_path / "orchestrator.db"))
    item = _item("failure")
    store.save_review_work_item(item)

    async def processor(work_item: ReviewWorkItem, settings: Settings):
        raise RuntimeError("controlled BB2 failure")

    response = asyncio.run(process_queued_review_item(item.id, Settings(), store, processor))
    reloaded = store.get_review_work_item(item.id)
    failures = build_recent_failures(store.list_review_work_items())
    worker_stats = build_worker_stats(store.list_review_work_items(), auto_processing_enabled=True)

    assert response is None
    assert reloaded is not None
    assert reloaded.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert reloaded.lifecycle_stage == ReviewLifecycleStage.REVIEW_FAILED
    assert reloaded.failure_count == 1
    assert reloaded.last_error == "controlled BB2 failure"
    assert failures[0].last_error == "controlled BB2 failure"
    assert worker_stats.failed_count == 1


def test_queue_worker_lifecycle_and_failure_stats() -> None:
    failed = _item("failed")
    record_lifecycle_stage(failed, ReviewLifecycleStage.WORKER_CLAIMED)
    failed.status = ReviewWorkItemStatus.PENDING_REVIEW
    record_lifecycle_stage(failed, ReviewLifecycleStage.REVIEW_FAILED, error="GitHub writeback failed")

    completed = _item("completed")
    completed.status = ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW
    record_lifecycle_stage(completed, ReviewLifecycleStage.GITHUB_WRITEBACK_STARTED)
    record_lifecycle_stage(completed, ReviewLifecycleStage.GITHUB_WRITEBACK_COMPLETED, success=True)
    record_lifecycle_stage(completed, ReviewLifecycleStage.REVIEW_COMPLETED)

    items = [failed, completed]

    queue_stats = build_queue_stats(items)
    worker_stats = build_worker_stats(items, auto_processing_enabled=True)
    lifecycle = build_lifecycle_visibility(items)
    failures = build_recent_failures(items)

    assert queue_stats.counters.review_queue_count == 2
    assert queue_stats.recent_failure_count == 1
    assert worker_stats.auto_processing_enabled is True
    assert worker_stats.claimed_count == 1
    assert worker_stats.completed_count == 1
    assert lifecycle[0].item_id == "failed"
    assert lifecycle[1].lifecycle_stage == ReviewLifecycleStage.REVIEW_COMPLETED
    assert lifecycle[1].github_writeback_started_at is not None
    assert lifecycle[1].github_writeback_completed_at is not None
    assert lifecycle[1].github_writeback_success is True
    assert failures[0].last_error == "GitHub writeback failed"


def test_sqlite_store_persists_observability_fields(tmp_path) -> None:
    store = SQLiteStateStore(str(tmp_path / "orchestrator.db"))
    item = _item()
    record_lifecycle_stage(item, ReviewLifecycleStage.WORKER_CLAIMED)
    record_lifecycle_stage(item, ReviewLifecycleStage.GITHUB_WRITEBACK_COMPLETED, success=False, error="Label write failed")
    store.save_review_work_item(item)

    reloaded = store.get_review_work_item(item.id)

    assert reloaded is not None
    assert reloaded.worker_claimed_at is not None
    assert reloaded.github_writeback_completed_at is not None
    assert reloaded.github_writeback_success is False
    assert reloaded.last_error == "Label write failed"


def test_diagnostics_endpoints_surface_lifecycle_and_failures(tmp_path) -> None:
    store = SQLiteStateStore(str(tmp_path / "orchestrator.db"))
    failed = _item("endpoint-failed")
    record_lifecycle_stage(failed, ReviewLifecycleStage.WORKER_CLAIMED)
    failed.status = ReviewWorkItemStatus.PENDING_REVIEW
    record_lifecycle_stage(failed, ReviewLifecycleStage.REVIEW_FAILED, error="diagnostic failure")
    store.save_review_work_item(failed)

    completed = _item("endpoint-completed")
    completed.status = ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW
    record_lifecycle_stage(completed, ReviewLifecycleStage.GITHUB_WRITEBACK_STARTED)
    record_lifecycle_stage(completed, ReviewLifecycleStage.GITHUB_WRITEBACK_COMPLETED, success=True)
    record_lifecycle_stage(completed, ReviewLifecycleStage.REVIEW_COMPLETED)
    store.save_review_work_item(completed)

    with TestClient(app) as client:
        app.state.storage = store

        queue_stats = client.get("/debug/review-queue/stats")
        worker_stats = client.get("/debug/workers/stats")
        lifecycle = client.get("/debug/review-lifecycle")
        failures = client.get("/debug/recent-failures")

    assert queue_stats.status_code == 200
    assert queue_stats.json()["counters"]["review_queue_count"] == 2
    assert queue_stats.json()["recent_failure_count"] == 1
    assert worker_stats.status_code == 200
    assert worker_stats.json()["claimed_count"] == 1
    assert worker_stats.json()["completed_count"] == 1
    assert worker_stats.json()["failed_count"] == 1
    assert lifecycle.status_code == 200
    lifecycle_by_id = {item["item_id"]: item for item in lifecycle.json()}
    assert lifecycle_by_id["endpoint-completed"]["lifecycle_stage"] == "review_completed"
    assert lifecycle_by_id["endpoint-completed"]["github_writeback_success"] is True
    assert failures.status_code == 200
    assert failures.json()[0]["item_id"] == "endpoint-failed"
    assert failures.json()[0]["last_error"] == "diagnostic failure"
