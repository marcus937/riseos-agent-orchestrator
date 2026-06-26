import io
import logging

from app.github_events import parse_github_event
from app.operational_logging import (
    LOGGER_NAME,
    configure_operational_logger,
    logger,
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


def test_operational_logger_has_info_level_stream_path() -> None:
    stream = io.StringIO()
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_disabled = logger.disabled
    original_propagate = logger.propagate
    try:
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)
        logger.disabled = False
        logger.propagate = False

        configured = configure_operational_logger(stream)
        log_event("logger_stream_probe", repo_full_name="riseos/example")

        assert configured is logging.getLogger(LOGGER_NAME)
        assert logger.level == logging.INFO
        assert logger.disabled is False
        assert logger.propagate is True
        assert any(getattr(handler, "_riseos_operational_handler", False) for handler in logger.handlers)
        assert '"event": "logger_stream_probe"' in stream.getvalue()
        assert '"repo_full_name": "riseos/example"' in stream.getvalue()
    finally:
        logger.handlers.clear()
        logger.handlers.extend(original_handlers)
        logger.setLevel(original_level)
        logger.disabled = original_disabled
        logger.propagate = original_propagate
