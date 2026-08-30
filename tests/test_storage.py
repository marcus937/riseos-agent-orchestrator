import json
import sqlite3

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


def test_duplicate_event_record_is_ignored(tmp_path) -> None:
    db_path = tmp_path / "orchestrator.db"
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
        },
    )
    record = event_record_from_parsed(parsed, event_id="delivery-1")
    store = SQLiteStateStore(str(db_path))

    first_saved = store.save_event_record(record)
    second_saved = store.save_event_record(record)

    assert first_saved is True
    assert second_saved is False
    assert store.has_event_record("delivery-1") is True
    assert store.event_count() == 1


def test_issue_dispatch_claim_is_single_owner(tmp_path) -> None:
    db_path = tmp_path / "orchestrator.db"
    store = SQLiteStateStore(str(db_path))

    first_claim = store.claim_issue_dispatch("riseos/example#7")
    second_claim = store.claim_issue_dispatch("riseos/example#7")

    assert first_claim is True
    assert second_claim is False
    assert store.already_dispatched("riseos/example#7") is True


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


def test_queue_item_runtime_validation_context_preserves_workflow_chain(tmp_path) -> None:
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
    workflow_chain = {
        "workflow_chain_id": "chain-1",
        "workflow_step": "WF21",
        "current_workflow_step": "WF21",
        "next_workflow_step": "WF22",
        "workflow_steps": ["WF21", "WF22", "WF23"],
    }
    item.runtime_validation_context = {
        "runtime_validation_id": "rv-1",
        "workflow_id": "chain-1",
        "workflow_chain": workflow_chain,
        "review_dispatch": {
            "workflow_chain": workflow_chain,
            "workflow_step": "WF21",
        },
    }
    store = SQLiteStateStore(str(db_path))

    store.save_review_work_item(item)
    with sqlite3.connect(db_path) as conn:
        raw_json = conn.execute(
            "SELECT runtime_validation_context FROM review_work_items WHERE id = ?",
            (item.id,),
        ).fetchone()[0]
    raw_context = json.loads(raw_json)
    reloaded = SQLiteStateStore(str(db_path)).get_review_work_item(item.id)

    assert raw_context["workflow_chain"]["workflow_chain_id"] == "chain-1"
    assert raw_context["review_dispatch"]["workflow_chain"]["current_workflow_step"] == "WF21"
    assert reloaded is not None
    assert reloaded.runtime_validation_context["workflow_chain"]["workflow_step"] == "WF21"
    assert reloaded.runtime_validation_context["review_dispatch"]["workflow_chain"]["next_workflow_step"] == "WF22"


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


def test_find_pending_duplicate_returns_claimed_item(tmp_path) -> None:
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
    claimed = store.claim_review_work_item(item.id)

    duplicate = store.find_pending_duplicate(candidate)

    assert claimed is not None
    assert claimed.status == ReviewWorkItemStatus.REVIEWING
    assert duplicate is not None
    assert duplicate.id == item.id


def test_claim_review_work_item_transitions_pending_to_reviewing(tmp_path) -> None:
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
    store = SQLiteStateStore(str(db_path))
    store.save_review_work_item(item)

    claimed = store.claim_review_work_item(item.id)
    second_claim = store.claim_review_work_item(item.id)

    assert claimed is not None
    assert claimed.status == ReviewWorkItemStatus.REVIEWING
    assert second_claim is None
    assert store.review_queue_counters().reviewing_count == 1


def test_reset_review_work_item_for_retry_returns_claimed_item_to_pending(tmp_path) -> None:
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
    store = SQLiteStateStore(str(db_path))
    store.save_review_work_item(item)
    store.claim_review_work_item(item.id)

    reset = store.reset_review_work_item_for_retry(item.id)

    assert reset is not None
    assert reset.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert store.review_queue_counters().pending_review_count == 1


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
