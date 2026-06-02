from app.event_store import event_record_from_parsed
from app.github_events import parse_github_event
from app.review_queue import ReviewWorkItemStatus, review_work_item_from_parsed
from app.storage import SQLiteStateStore, build_sqlite_store


def test_event_persists_and_reloads(tmp_path) -> None:
    db_path = tmp_path / "orchestrator.db"
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
        },
    )
    record = event_record_from_parsed(parsed)

    SQLiteStateStore(str(db_path)).save_event_record(record)
    reloaded = SQLiteStateStore(str(db_path)).recent_events()

    assert len(reloaded) == 1
    assert reloaded[0].event_id == record.event_id
    assert reloaded[0].repo_full_name == "riseos/example"


def test_queue_item_persists_and_reloads(tmp_path) -> None:
    db_path = tmp_path / "orchestrator.db"
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "def456"}},
        },
    )
    item = review_work_item_from_parsed(parsed)

    SQLiteStateStore(str(db_path)).save_review_work_item(item)
    reloaded = SQLiteStateStore(str(db_path)).get_review_work_item(item.id)

    assert reloaded is not None
    assert reloaded.id == item.id
    assert reloaded.pr_number == 7
    assert reloaded.status == "pending_review"


def test_find_pending_duplicate_returns_existing_item(tmp_path) -> None:
    db_path = tmp_path / "orchestrator.db"
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
        },
    )
    item = review_work_item_from_parsed(parsed)
    candidate = review_work_item_from_parsed(parsed)
    store = SQLiteStateStore(str(db_path))
    store.save_review_work_item(item)

    duplicate = store.find_pending_duplicate(candidate)

    assert duplicate is not None
    assert duplicate.id == item.id


def test_queue_limit_prunes_oldest_processed_items(tmp_path) -> None:
    db_path = tmp_path / "orchestrator.db"
    store = SQLiteStateStore(str(db_path), max_review_items=2)

    for index in range(3):
        parsed = parse_github_event(
            "push",
            {
                "repository": {"full_name": "riseos/example"},
                "ref": "refs/heads/agent-integration",
                "after": f"abc{index}",
            },
        )
        item = review_work_item_from_parsed(parsed)
        item.status = ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW
        store.save_review_work_item(item)

    items = SQLiteStateStore(str(db_path), max_review_items=2).list_review_work_items()

    assert len(items) == 2
    assert {item.commit_sha for item in items} == {"abc1", "abc2"}


def test_missing_db_path_uses_memory_fallback() -> None:
    assert build_sqlite_store(None) is None
    assert build_sqlite_store("") is None
