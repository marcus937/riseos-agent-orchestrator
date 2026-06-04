from datetime import UTC, datetime

from app.github_events import GitHubEventType
from app.review_queue import (
    ReviewLifecycleStage,
    ReviewWorkItem,
    ReviewWorkItemStatus,
    build_lifecycle_visibility,
    build_queue_stats,
    build_recent_failures,
    build_worker_stats,
    record_lifecycle_stage,
)
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


def test_lifecycle_stage_records_failure_text() -> None:
    item = _item()

    record_lifecycle_stage(item, ReviewLifecycleStage.REVIEW_FAILED, error="OpenAI timeout")

    assert item.lifecycle_stage == ReviewLifecycleStage.REVIEW_FAILED
    assert item.failure_count == 1
    assert item.last_error == "OpenAI timeout"
    assert item.last_failure_at is not None


def test_queue_worker_lifecycle_and_failure_stats() -> None:
    failed = _item("failed")
    record_lifecycle_stage(failed, ReviewLifecycleStage.WORKER_CLAIMED)
    failed.status = ReviewWorkItemStatus.PENDING_REVIEW
    record_lifecycle_stage(failed, ReviewLifecycleStage.REVIEW_FAILED, error="GitHub writeback failed")

    completed = _item("completed")
    completed.status = ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW
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