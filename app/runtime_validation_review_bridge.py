from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.circuit_runtime_validation import RuntimeValidationResult, stable_validation_digest
from app.config import Settings
from app.github_events import GitHubEventType, ParsedGitHubEvent
from app.operational_logging import log_event
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
IMMUTABLE_CORRELATION_KEYS = ("deployment_id", "workflow_id", "correlation_id")


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
    log_event(
        "runtime_validation_context_attached_to_review_item",
        work_item_id=item.id,
        agent_bus_work_item_id=result.work_item_id,
        workflow_id=result.workflow_id,
        runtime_validation_id=result.validation_id,
        evidence_packet_id=result.evidence_id,
        repository=result.repo,
        pr_number=result.pr_number,
        branch=result.branch,
        commit_sha=_commit_sha_from_result(result),
        target_url=result.hermes.target_url,
        hermes_job_id=result.hermes.job_id,
        terminal_status=result.status,
        gate_lookup_key=_gate_lookup_key(result),
    )

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
        "work_item_id": result.work_item_id,
        "agent_bus_work_item_id": result.work_item_id,
        "evidence_id": result.evidence_id,
        "evidence_packet_id": result.evidence_id,
        "workflow_id": result.workflow_id,
        "correlation_id": result.correlation_id,
        "created_at": result.created_at.isoformat(),
        "completed_at": result.completed_at.isoformat() if result.completed_at else None,
        "error": result.error,
        "hermes_job_id": result.hermes.job_id,
        "hermes_status": result.hermes.status,
        "target_url": result.hermes.target_url,
        "target_source": result.hermes.target_source,
        "screenshot_available": result.evidence.screenshot_present,
        "console_errors": result.evidence.console_error_count,
        "console_warnings": result.evidence.console_warning_count,
        "network_failures": result.evidence.network_failure_count,
        "network_non_2xx": result.evidence.network_non_2xx_count,
        "evidence_artifacts": result.evidence.artifacts,
        "gate_lookup_key": _gate_lookup_key(result),
        "review_dispatch": result.review_dispatch,
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
    items = storage.list_review_work_items() if storage is not None else review_queue.list_items()
    for existing in items:
        if existing.status not in {
            ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING,
            ReviewWorkItemStatus.PENDING_REVIEW,
            ReviewWorkItemStatus.REVIEWING,
        }:
            continue
        if _runtime_items_correlate(existing, item):
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


def _runtime_items_correlate(existing: ReviewWorkItem, candidate: ReviewWorkItem) -> bool:
    if existing.repo_full_name != candidate.repo_full_name:
        return False
    if existing.event_type != candidate.event_type:
        return False

    existing_context = existing.runtime_validation_context if isinstance(existing.runtime_validation_context, dict) else {}
    candidate_context = candidate.runtime_validation_context if isinstance(candidate.runtime_validation_context, dict) else {}
    for key in IMMUTABLE_CORRELATION_KEYS:
        existing_value = _context_value(existing_context, key)
        candidate_value = _context_value(candidate_context, key)
        if existing_value and candidate_value:
            return existing_value == candidate_value

    if existing.commit_sha and candidate.commit_sha:
        return existing.commit_sha == candidate.commit_sha
    if existing.pr_number is not None and candidate.pr_number is not None:
        return existing.pr_number == candidate.pr_number
    if existing.branch and candidate.branch and existing.branch == candidate.branch:
        return existing.pr_number is None or candidate.pr_number is None or existing.pr_number == candidate.pr_number
    return review_work_item_identity(existing) == review_work_item_identity(candidate)


def _context_value(context: dict[str, Any], key: str) -> str | None:
    value = context.get(key)
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _gate_lookup_key(result: RuntimeValidationResult) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "work_item_id": result.work_item_id,
            "workflow_id": result.workflow_id,
            "repository": result.repo,
            "pr_number": result.pr_number,
            "branch": result.branch,
            "commit_sha": _commit_sha_from_result(result),
        }.items()
        if value is not None
    }


def _commit_sha_from_result(result: RuntimeValidationResult) -> str | None:
    review_dispatch = result.review_dispatch if isinstance(result.review_dispatch, dict) else {}
    value = review_dispatch.get("commit_sha") or review_dispatch.get("commitSha")
    return str(value) if value else None
