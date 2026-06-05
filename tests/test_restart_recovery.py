from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.github_events import parse_github_event
from app.main import app
from app.review_queue import ReviewWorkItemStatus, review_work_item_from_parsed
from app.storage import SQLiteStateStore


def _claimed_item(store: SQLiteStateStore):
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
        },
    )
    item = review_work_item_from_parsed(parsed)
    store.save_review_work_item(item)
    claimed = store.claim_review_work_item(item.id)
    assert claimed is not None
    return claimed


def test_reclaim_stale_review_claim_returns_old_reviewing_item_to_pending(tmp_path) -> None:
    store = SQLiteStateStore(str(tmp_path / "orchestrator.db"))
    claimed = _claimed_item(store)
    claimed.worker_claimed_at = datetime.now(UTC) - timedelta(minutes=30)
    claimed.updated_at = claimed.worker_claimed_at
    store.save_review_work_item(claimed)

    recovered = store.reclaim_stale_review_claims(older_than_seconds=900)

    assert len(recovered) == 1
    assert recovered[0].id == claimed.id
    assert recovered[0].status == ReviewWorkItemStatus.PENDING_REVIEW
    assert recovered[0].failure_count == 1
    assert recovered[0].last_error == "Recovered stale worker claim after restart."
    assert store.review_queue_counters().pending_review_count == 1


def test_reclaim_stale_review_claim_keeps_recent_reviewing_item_claimed(tmp_path) -> None:
    store = SQLiteStateStore(str(tmp_path / "orchestrator.db"))
    claimed = _claimed_item(store)

    recovered = store.reclaim_stale_review_claims(older_than_seconds=900)
    reloaded = store.get_review_work_item(claimed.id)

    assert recovered == []
    assert reloaded is not None
    assert reloaded.status == ReviewWorkItemStatus.REVIEWING
    assert reloaded.failure_count == 0


def test_startup_reclaims_stale_sqlite_claim_after_restart(tmp_path) -> None:
    db_path = tmp_path / "orchestrator.db"
    store = SQLiteStateStore(str(db_path))
    claimed = _claimed_item(store)
    claimed.worker_claimed_at = datetime.now(UTC) - timedelta(minutes=30)
    claimed.updated_at = claimed.worker_claimed_at
    store.save_review_work_item(claimed)

    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: Settings(
        github_webhook_secret="test-secret",
        orchestrator_db_path=str(db_path),
        review_claim_timeout_seconds=900,
    )
    try:
        with TestClient(app) as client:
            queue = client.get("/debug/review-queue").json()
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert len(queue) == 1
    assert queue[0]["id"] == claimed.id
    assert queue[0]["status"] == ReviewWorkItemStatus.PENDING_REVIEW.value
    assert queue[0]["failure_count"] == 1
    assert queue[0]["last_error"] == "Recovered stale worker claim after restart."
