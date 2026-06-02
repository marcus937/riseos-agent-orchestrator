import json
import logging
from typing import Any

from app.github_events import ParsedGitHubEvent
from app.review_queue import ReviewWorkItem


logger = logging.getLogger("riseos_agent_orchestrator")


def log_event(event: str, **fields: Any) -> None:
    payload = {"event": event, **{key: value for key, value in fields.items() if value is not None}}
    logger.info(json.dumps(payload, sort_keys=True, default=str))


def log_webhook_accepted(parsed: ParsedGitHubEvent) -> None:
    log_event(
        "webhook_accepted",
        github_event=str(parsed.event_type),
        repo_full_name=parsed.repository,
        action=parsed.action,
        commit_sha=parsed.head_sha,
        issue_number=parsed.issue_number,
        pr_number=parsed.pull_request_number,
    )


def log_queue_item_created(item: ReviewWorkItem) -> None:
    log_event(
        "queue_item_created",
        item_id=item.id,
        repo_full_name=item.repo_full_name,
        event_type=str(item.event_type),
        commit_sha=item.commit_sha,
        issue_number=item.issue_number,
        pr_number=item.pr_number,
        status=str(item.status),
    )


def log_review_processing_started(item: ReviewWorkItem) -> None:
    log_event(
        "review_processing_started",
        item_id=item.id,
        repo_full_name=item.repo_full_name,
        event_type=str(item.event_type),
        commit_sha=item.commit_sha,
        issue_number=item.issue_number,
        pr_number=item.pr_number,
    )


def log_openai_review_attempted(*, reviewer_model: str | None) -> None:
    log_event("openai_review_attempted", attempted=True, reviewer_model=reviewer_model)


def log_openai_review_result(*, attempted: bool, success: bool, error: str | None, reviewer_model: str | None) -> None:
    if not attempted:
        return
    log_event(
        "openai_review_succeeded" if success else "openai_review_failed",
        attempted=attempted,
        success=success,
        error=error,
        reviewer_model=reviewer_model,
    )


def log_github_writeback_attempted() -> None:
    log_event("github_writeback_attempted", attempted=True)


def log_github_writeback_result(*, attempted: bool, success: bool, error: str | None) -> None:
    if not attempted:
        return
    log_event(
        "github_writeback_succeeded" if success else "github_writeback_failed",
        attempted=attempted,
        success=success,
        error=error,
    )
