from app.github_events import parse_github_event
from app.review_queue import InMemoryReviewQueue, ReviewWorkItemStatus
from app.review_workflow import build_review_workflow


def _agent_integration_push(after: str = "abc123"):
    return parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": after,
        },
    )


def test_queue_does_not_add_duplicate_pending_item() -> None:
    queue = InMemoryReviewQueue()
    parsed = _agent_integration_push()
    workflow = build_review_workflow(parsed)

    first = queue.create_from_review_event(parsed, workflow)
    second = queue.create_from_review_event(parsed, workflow)

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert len(queue.list_items()) == 1


def test_queue_does_not_add_duplicate_claimed_item() -> None:
    queue = InMemoryReviewQueue()
    parsed = _agent_integration_push()
    workflow = build_review_workflow(parsed)
    first = queue.create_from_review_event(parsed, workflow)

    claimed = queue.claim_item(first.id)
    second = queue.create_from_review_event(parsed, workflow)

    assert claimed is not None
    assert claimed.status == ReviewWorkItemStatus.REVIEWING
    assert second is not None
    assert second.id == first.id
    assert len(queue.list_items()) == 1


def test_queue_claim_transitions_pending_item_to_reviewing_once() -> None:
    queue = InMemoryReviewQueue()
    parsed = _agent_integration_push()
    item = queue.create_from_review_event(parsed, build_review_workflow(parsed))

    claimed = queue.claim_item(item.id)
    second_claim = queue.claim_item(item.id)

    assert claimed is not None
    assert claimed.status == ReviewWorkItemStatus.REVIEWING
    assert second_claim is None
    assert queue.counters().reviewing_count == 1


def test_queue_reset_item_for_retry_returns_reviewing_item_to_pending() -> None:
    queue = InMemoryReviewQueue()
    parsed = _agent_integration_push()
    item = queue.create_from_review_event(parsed, build_review_workflow(parsed))
    queue.claim_item(item.id)

    reset = queue.reset_item_for_retry(item.id)

    assert reset is not None
    assert reset.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert queue.counters().pending_review_count == 1


def test_queue_limit_prunes_oldest_processed_memory_item() -> None:
    queue = InMemoryReviewQueue()
    for index in range(3):
        parsed = _agent_integration_push(f"abc{index}")
        item = queue.create_from_review_event(parsed, build_review_workflow(parsed))
        item.status = ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW

    removed = queue.prune_processed(2)

    assert removed == 1
    assert len(queue.list_items()) == 2
    assert {item.commit_sha for item in queue.list_items()} == {"abc1", "abc2"}
