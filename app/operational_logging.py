import json
import logging
from typing import Any

from app.correlation import correlation_id_from_item, correlation_id_from_parsed
from app.github_events import ParsedGitHubEvent
from app.review_queue import ReviewWorkItem
from app.slack_issue_dispatch import SlackIssueDispatchResult


logger = logging.getLogger("riseos_agent_orchestrator")


def log_event(event: str, **fields: Any) -> None:
    payload = {"event": event, **{key: value for key, value in fields.items() if value is not None}}
    logger.info(json.dumps(payload, sort_keys=True, default=str))


def log_webhook_accepted(parsed: ParsedGitHubEvent) -> None:
    log_event(
        "webhook_accepted",
        correlation_id=correlation_id_from_parsed(parsed),
        github_event=str(parsed.event_type),
        repo_full_name=parsed.repository,
        action=parsed.action,
        commit_sha=parsed.head_sha,
        issue_number=parsed.issue_number,
        pr_number=parsed.pull_request_number,
    )


def log_webhook_duplicate_suppressed(parsed: ParsedGitHubEvent, *, event_id: str) -> None:
    log_event(
        "webhook_duplicate_suppressed",
        event_id=event_id,
        duplicate_source=_duplicate_source(event_id),
        correlation_id=correlation_id_from_parsed(parsed),
        github_event=str(parsed.event_type),
        repo_full_name=parsed.repository,
        action=parsed.action,
        commit_sha=parsed.head_sha,
        issue_number=parsed.issue_number,
        pr_number=parsed.pull_request_number,
    )


def log_queue_item_created(item: ReviewWorkItem) -> None:
    log_event(
        "review_queued",
        correlation_id=correlation_id_from_item(item),
        item_id=item.id,
        repo_full_name=item.repo_full_name,
        event_type=str(item.event_type),
        commit_sha=item.commit_sha,
        issue_number=item.issue_number,
        pr_number=item.pr_number,
        status=str(item.status),
    )


def log_worker_claimed(item: ReviewWorkItem) -> None:
    log_event(
        "worker_claimed",
        correlation_id=correlation_id_from_item(item),
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
        "review_started",
        correlation_id=correlation_id_from_item(item),
        item_id=item.id,
        repo_full_name=item.repo_full_name,
        event_type=str(item.event_type),
        commit_sha=item.commit_sha,
        issue_number=item.issue_number,
        pr_number=item.pr_number,
    )


def log_review_completed(item: ReviewWorkItem, *, decision: str | None = None) -> None:
    log_event(
        "review_completed",
        correlation_id=correlation_id_from_item(item),
        item_id=item.id,
        repo_full_name=item.repo_full_name,
        event_type=str(item.event_type),
        commit_sha=item.commit_sha,
        issue_number=item.issue_number,
        pr_number=item.pr_number,
        status=str(item.status),
        decision=decision,
    )


def log_review_failed(item: ReviewWorkItem, *, error: str | None = None) -> None:
    log_event(
        "review_failed",
        correlation_id=correlation_id_from_item(item),
        item_id=item.id,
        repo_full_name=item.repo_full_name,
        event_type=str(item.event_type),
        commit_sha=item.commit_sha,
        issue_number=item.issue_number,
        pr_number=item.pr_number,
        status=str(item.status),
        error=error,
    )


def log_auto_review_processing_started(item: ReviewWorkItem) -> None:
    log_event(
        "auto_review_processing_started",
        correlation_id=correlation_id_from_item(item),
        item_id=item.id,
        repo_full_name=item.repo_full_name,
        event_type=str(item.event_type),
        commit_sha=item.commit_sha,
        issue_number=item.issue_number,
        pr_number=item.pr_number,
        status=str(item.status),
    )


def log_auto_review_processing_result(
    item: ReviewWorkItem,
    *,
    success: bool,
    error: str | None = None,
    decision: str | None = None,
) -> None:
    log_event(
        "auto_review_processing_succeeded" if success else "auto_review_processing_failed",
        correlation_id=correlation_id_from_item(item),
        item_id=item.id,
        repo_full_name=item.repo_full_name,
        event_type=str(item.event_type),
        commit_sha=item.commit_sha,
        issue_number=item.issue_number,
        pr_number=item.pr_number,
        status=str(item.status),
        success=success,
        decision=decision,
        error=error,
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
    log_event("github_writeback_started", attempted=True)


def log_github_writeback_result(*, attempted: bool, success: bool, error: str | None) -> None:
    if not attempted:
        return
    log_event(
        "github_writeback_completed",
        attempted=attempted,
        success=success,
        error=error,
    )


def log_slack_issue_dispatch_result(parsed: ParsedGitHubEvent, result: SlackIssueDispatchResult) -> None:
    event_name = _slack_dispatch_event_name(result)
    log_event(
        event_name,
        attempted=result.attempted,
        success=result.success,
        issue_key=result.issue_key,
        correlation_id=result.correlation_id or correlation_id_from_parsed(parsed),
        repo_full_name=parsed.repository,
        issue_number=parsed.issue_number,
        action=parsed.action,
        skipped_reason=result.skipped_reason,
        error=result.error,
    )


def _slack_dispatch_event_name(result: SlackIssueDispatchResult) -> str:
    if result.success:
        return "slack_issue_dispatch_succeeded"
    if result.error:
        return "slack_issue_dispatch_failed"
    if result.skipped_reason == "Issue was already dispatched.":
        return "slack_issue_dispatch_duplicate_suppressed"
    if result.skipped_reason == "Slack dispatch is not configured.":
        return "slack_issue_dispatch_missing_config"
    if result.skipped_reason == "Repository is not approved for Circuit Slack dispatch.":
        return "slack_issue_dispatch_invalid_repo"
    return "slack_issue_dispatch_skipped"


def _duplicate_source(event_id: str) -> str:
    if event_id.startswith("github-delivery:"):
        return "github_delivery_header"
    if event_id.startswith("github-derived:"):
        return "derived_webhook_identity"
    return "unknown"
