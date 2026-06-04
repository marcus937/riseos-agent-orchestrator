from app.github_events import parse_github_event
from app.operational_logging import (
    log_event,
    log_github_writeback_attempted,
    log_github_writeback_result,
    log_openai_review_attempted,
    log_openai_review_result,
    log_queue_item_created,
    log_review_completed,
    log_review_failed,
    log_review_processing_started,
    log_webhook_accepted,
    log_worker_claimed,
)
from app.review_queue import review_work_item_from_parsed


def test_structured_logging_functions_are_callable() -> None:
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
        },
    )
    item = review_work_item_from_parsed(parsed)

    log_event("test_event", repo_full_name="riseos/example")
    log_webhook_accepted(parsed)
    log_queue_item_created(item)
    log_worker_claimed(item)
    log_review_processing_started(item)
    log_review_completed(item, decision="APPROVED_FOR_HUMAN_REVIEW")
    log_review_failed(item, error="review failed")
    log_openai_review_attempted(reviewer_model="mock-model")
    log_openai_review_result(attempted=True, success=True, error=None, reviewer_model="mock-model")
    log_openai_review_result(attempted=True, success=False, error="bad json", reviewer_model="mock-model")
    log_github_writeback_attempted()
    log_github_writeback_result(attempted=True, success=True, error=None)
    log_github_writeback_result(attempted=True, success=False, error="GitHub failed")
