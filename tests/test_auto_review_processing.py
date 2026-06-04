import asyncio
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.github_events import GitHubEventType
from app.main import _auto_process_work_item
from app.review_queue import ReviewProcessResponse, ReviewWorkItem, ReviewWorkItemStatus, process_review_work_item


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeStorage:
    def __init__(self) -> None:
        self.saved_items: list[ReviewWorkItem] = []

    def save_review_work_item(self, item: ReviewWorkItem) -> None:
        self.saved_items.append(item)


def review_item() -> ReviewWorkItem:
    return ReviewWorkItem(
        id="review-item-1",
        created_at=datetime.now(UTC),
        repo_full_name="riseos/example",
        event_type=GitHubEventType.PULL_REQUEST,
        branch="agent-integration",
        commit_sha="abc123",
        pr_number=7,
    )


def test_auto_processing_disabled_does_not_process_or_persist() -> None:
    item = review_item()
    storage = FakeStorage()

    result = run(_auto_process_work_item(item, Settings(enable_auto_review_processing=False), storage))

    assert result is None
    assert item.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert storage.saved_items == []


def test_auto_processing_runs_and_persists_processed_item(monkeypatch: Any) -> None:
    async def fake_process(item: ReviewWorkItem, settings: Settings) -> ReviewProcessResponse:
        return process_review_work_item(item)

    monkeypatch.setattr("app.main._process_work_item", fake_process)
    item = review_item()
    storage = FakeStorage()

    result = run(_auto_process_work_item(item, Settings(enable_auto_review_processing=True), storage))

    assert result is not None
    assert result.work_item.status == ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW
    assert storage.saved_items == [result.work_item]
