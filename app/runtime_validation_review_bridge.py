from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.circuit_runtime_validation import RuntimeValidationResult, stable_validation_digest
from app.config import Settings
from app.github_events import GitHubEventType, ParsedGitHubEvent
from app.review_queue import (
    ReviewLifecycleStage,
    ReviewWorkItem,
    ReviewWorkItemStatus,
    record_lifecycle_stage,
    review_queue,
    review_work_item_from_parsed,
    review_work_item_identity,
)

TERMINAL_RUNTIME_VALIDATION_STATUSES = {"blocked", "completed", "failed"}
RUNTIME_REVIEW_SOURCE = "runtime_validation_bb2_packet"


def create_runtime_validation_pending_item(parsed: ParsedGitHubEvent) -> ReviewWorkItem:
    item = review_work_item_from_parsed(parsed)
    item.status = ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING
    record_lifecycle_stage(item, ReviewLifecycleStage.RUNTIME_VALIDATION_PENDING)
    return item


def enqueue_runtime_pending_item(item: ReviewWorkItem, *, storage: Any | None = None, max_review_items: int = 500) -> ReviewWorkItem:
    duplicate = _find_existing_runtime_item(item, storage=storage)
    if duplicate is not None:
        return duplicate
    if storage is not None:
        storage.save_review_work_item(item)
        return item
    queued = review_queue.add_if_absent(item)
    review_queue.prune_processed(max_review_items)
    return queued


def enqueue_review_from_runtime_validation(
    result: RuntimeValidationResult,
    settings: Settings,
    *,
    storage: Any | None = None,
    existing_item: ReviewWorkItem | None = None,
) -> ReviewWorkItem | None:
    if not settings.enable_runtime_validation_review_bridge:
        return None
    if result.status not in TERMINAL_RUNTIME_VALIDATION_STATUSES:
        return None

    digest = stable_validation_digest(result)
    duplicate = _find_exact_runtime_result(result, digest=digest, storage=storage)
    if duplicate is not None:
        result.bb2.packet_created = True
        result.bb2.review_requested = True
        return duplicate

    item = existing_item or _find_pending_runtime_result(result, storage=storage) or _review_work_item_from_runtime_validation(result)
    _attach_runtime_validation_context(item, result, digest=digest)

    terminal_stage = ReviewLifecycleStage.RUNTIME_VALIDATION_COMPLETED if result.status == "completed" else ReviewLifecycleStage.RUNTIME_VALIDATION_FAILED
    record_lifecycle_stage(item, terminal_stage, error=result.error)
    item.status = ReviewWorkItemStatus.PENDING_REVIEW
    record_lifecycle_stage(item, ReviewLifecycleStage.BB2_REVIEW_REQUESTED_FROM_RUNTIME_VALIDATION)
    result.bb2.packet_created = True
    result.bb2.review_requested = True

    if storage is not None:
        storage.save_review_work_item(item)
        return item
    queued = review_queue.add_if_absent(item)
    return queued


def runtime_validation_context_from_result(result: RuntimeValidationResult) -> dict[str, object]:
    evidence = result.evidence.model_dump(mode="json")
    hermes = result.hermes.model_dump(mode="json")
    bb2 = result.bb2.model_dump(mode="json")
    return {
        "source": RUNTIME_REVIEW_SOURCE,
        "validation_id": result.validation_id,
        "validation_status": result.status,
        "validation_type": result.validation_type,
        "repo": result.repo,
        "issue_number": result.issue_number,
        "pr_number": result.pr_number,
        "branch": result.branch,
        "base_branch": result.base_branch,
        "correlation_id": result.correlation_id,
        "created_at": result.created_at.isoformat(),
        "completed_at": result.completed_at.isoformat() if result.completed_at else None,
        "error": result.error,
        "hermes_status": result.hermes.status,
        "target_url": result.hermes.target_url,
        "target_source": result.hermes.target_source,
        "screenshot_available": result.evidence.screenshot_present,
        "console_errors": result.evidence.console_error_count,
        "console_warnings": result.evidence.console_warning_count,
        "network_failures": result.evidence.network_failure_count,
        "network_non_2xx": result.evidence.network_non_2xx_count,
        "evidence_artifacts": result.evidence.artifacts,
        "hermes": hermes,
        "evidence": evidence,
        "bb2_packet": bb2,
    }


def _review_work_item_from_runtime_validation(result: RuntimeValidationResult) -> ReviewWorkItem:
    now = datetime.now(UTC)
    return ReviewWorkItem(
        id=str(uuid4()),
        created_at=now,
        updated_at=now,
        repo_full_name=result.repo,
        event_type=GitHubEventType.PULL_REQUEST if result.pr_number is not None else GitHubEventType.ISSUES,
        branch=result.branch,
        base_branch=result.base_branch,
        issue_number=result.issue_number,
        pr_number=result.pr_number,
        labels=["bb-review-needed", "runtime-agent"],
    )


def _attach_runtime_validation_context(item: ReviewWorkItem, result: RuntimeValidationResult, *, digest: str) -> None:
    item.repo_full_name = item.repo_full_name or result.repo
    item.branch = item.branch or result.branch
    item.base_branch = item.base_branch or result.base_branch
    item.issue_number = item.issue_number or result.issue_number
    item.pr_number = item.pr_number or result.pr_number
    if "bb-review-needed" not in item.labels:
        item.labels = sorted({*item.labels, "bb-review-needed"})
    item.runtime_validation_id = result.validation_id
    item.runtime_validation_status = result.status
    item.runtime_validation_digest = digest
    item.runtime_validation_completed_at = result.completed_at
    item.runtime_validation_context = runtime_validation_context_from_result(result)


def _find_existing_runtime_item(item: ReviewWorkItem, *, storage: Any | None = None) -> ReviewWorkItem | None:
    identity = review_work_item_identity(item)
    items = storage.list_review_work_items() if storage is not None else review_queue.list_items()
    for existing in items:
        if review_work_item_identity(existing) == identity and existing.status in {
            ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING,
            ReviewWorkItemStatus.PENDING_REVIEW,
            ReviewWorkItemStatus.REVIEWING,
        }:
            return existing
    return None


def _find_exact_runtime_result(result: RuntimeValidationResult, *, digest: str, storage: Any | None = None) -> ReviewWorkItem | None:
    items = storage.list_review_work_items() if storage is not None else review_queue.list_items()
    for item in items:
        if item.runtime_validation_id == result.validation_id or item.runtime_validation_digest == digest:
            return item
    return None


def _find_pending_runtime_result(result: RuntimeValidationResult, *, storage: Any | None = None) -> ReviewWorkItem | None:
    items = storage.list_review_work_items() if storage is not None else review_queue.list_items()
    for item in items:
        if (
            item.repo_full_name == result.repo
            and item.pr_number == result.pr_number
            and item.issue_number == result.issue_number
            and item.branch == result.branch
            and item.status == ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING
        ):
            return item
    return None
