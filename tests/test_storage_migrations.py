import sqlite3
from datetime import UTC, datetime

from app.github_events import GitHubEventType
from app.review_queue import ReviewWorkItem, ReviewWorkItemStatus
from app.storage import SQLiteStateStore


def test_legacy_review_work_item_rows_load_after_additive_migration(tmp_path) -> None:
    db_path = tmp_path / "orchestrator.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE review_work_items (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                repo_full_name TEXT,
                event_type TEXT NOT NULL,
                branch TEXT,
                commit_sha TEXT,
                issue_number INTEGER,
                pr_number INTEGER,
                status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO review_work_items (
                id, created_at, repo_full_name, event_type, branch, commit_sha,
                issue_number, pr_number, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-item",
                datetime.now(UTC).isoformat(),
                "riseos/example",
                GitHubEventType.PULL_REQUEST.value,
                "agent-integration",
                "abc123",
                None,
                111,
                ReviewWorkItemStatus.PENDING_REVIEW.value,
            ),
        )

    store = SQLiteStateStore(str(db_path))
    item = store.get_review_work_item("legacy-item")

    assert item is not None
    assert item.runtime_validation_id is None
    assert item.runtime_validation_status is None
    assert item.runtime_validation_context == {}
    assert item.failure_count == 0


def test_runtime_validation_columns_are_additive_and_round_trip(tmp_path) -> None:
    store = SQLiteStateStore(str(tmp_path / "orchestrator.db"))
    item = ReviewWorkItem(
        id="runtime-item",
        created_at=datetime.now(UTC),
        repo_full_name="riseos/example",
        event_type=GitHubEventType.PULL_REQUEST,
        branch="agent-integration",
        pr_number=111,
        status=ReviewWorkItemStatus.PENDING_REVIEW,
        runtime_validation_id="validation-1",
        runtime_validation_status="completed",
        runtime_validation_context={"validation_status": "completed", "console_errors": 0},
    )

    store.save_review_work_item(item)
    reloaded = store.get_review_work_item("runtime-item")

    assert reloaded is not None
    assert reloaded.runtime_validation_id == "validation-1"
    assert reloaded.runtime_validation_status == "completed"
    assert reloaded.runtime_validation_context == {"validation_status": "completed", "console_errors": 0}
