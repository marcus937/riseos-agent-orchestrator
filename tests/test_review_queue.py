from app.github_events import parse_github_event
from app.review_queue import InMemoryReviewQueue, ReviewWorkItemStatus
from app.review_workflow import build_review_workflow


def test_queue_does_not_add_duplicate_pending_item() -> None:
    queue = InMemoryReviewQueue()
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
        },
    )
    workflow = build_review_workflow(parsed)

    first = queue.create_from_review_event(parsed, workflow)
    second = queue.create_from_review_event(parsed, workflow)

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert len(queue.list_items()) == 1


def test_queue_limit_prunes_oldest_processed_memory_item() -> None:
    queue = InMemoryReviewQueue()
    for index in range(3):
        parsed = parse_github_event(
            "push",
            {
                "repository": {"full_name": "riseos/example"},
                "ref": "refs/heads/agent-integration",
                "after": f"abc{index}",
            },
        )
        item = queue.create_from_review_event(parsed, build_review_workflow(parsed))
        item.status = ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW

    removed = queue.prune_processed(2)

    assert removed == 1
    assert len(queue.list_items()) == 2
    assert {item.commit_sha for item in queue.list_items()} == {"abc1", "abc2"}
