import asyncio
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.github_events import GitHubEventType
from app.main import _schedule_auto_process_work_item
from app.review_queue import ReviewProcessResponse, ReviewWorkItem, ReviewWorkItemStatus, process_review_work_item, review_queue
from app.review_worker import process_queued_review_item


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeBackgroundTasks:
    def __init__(self) -> None:
        self.tasks: list[tuple[Any, tuple[Any, ...]]] = []

    def add_task(self, func: Any, *args: Any, **kwargs: Any) -> None:
        self.tasks.append((func, args))


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


def test_auto_processing_disabled_does_not_schedule_or_process() -> None:
    item = review_item()
    background_tasks = FakeBackgroundTasks()

    scheduled = _schedule_auto_process_work_item(
        item,
        Settings(enable_auto_review_processing=False),
        None,
        background_tasks,
    )

    assert scheduled is False
    assert background_tasks.tasks == []
    assert item.status == ReviewWorkItemStatus.PENDING_REVIEW


def test_auto_processing_schedules_background_worker_without_processing_inline() -> None:
    item = review_queue.add_if_absent(review_item())
    background_tasks = FakeBackgroundTasks()

    scheduled = _schedule_auto_process_work_item(
        item,
        Settings(enable_auto_review_processing=True),
        None,
        background_tasks,
    )

    assert scheduled is True
    assert item.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert len(background_tasks.tasks) == 1
    task, args = background_tasks.tasks[0]
    assert task is process_queued_review_item
    assert args[0] == item.id
    review_queue.reset()


def test_background_worker_claims_and_persists_processed_item() -> None:
    async def fake_process(item: ReviewWorkItem, settings: Settings) -> ReviewProcessResponse:
        return process_review_work_item(item)

    item = review_queue.add_if_absent(review_item())

    result = run(process_queued_review_item(item.id, Settings(enable_auto_review_processing=True), None, fake_process))

    assert result is not None
    assert result.work_item.status == ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW
    assert review_queue.get_item(item.id).status == ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW
    review_queue.reset()


def test_background_worker_resets_claimed_item_for_retry_on_failure() -> None:
    async def failing_process(item: ReviewWorkItem, settings: Settings) -> ReviewProcessResponse:
        raise RuntimeError("review service unavailable")

    item = review_queue.add_if_absent(review_item())

    result = run(process_queued_review_item(item.id, Settings(enable_auto_review_processing=True), None, failing_process))

    assert result is None
    assert review_queue.get_item(item.id).status == ReviewWorkItemStatus.PENDING_REVIEW
    review_queue.reset()
