from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.circuit_runtime_validation import RuntimeValidationRequest
from app.github_events import ParsedGitHubEvent
from app.operational_logging import log_event

REJECTION_REASON_SHA_MISMATCH = "SHA_MISMATCH"
REJECTION_REASON_BRANCH_MISMATCH = "BRANCH_MISMATCH"
REJECTION_REASON_PR_MISMATCH = "PR_MISMATCH"
REJECTION_REASON_REPOSITORY_MISMATCH = "REPOSITORY_MISMATCH"
REJECTION_REASON_STATE_NOT_WAITING = "STATE_NOT_WAITING"
REJECTION_REASON_ALREADY_RESUMED = "ALREADY_RESUMED"
REJECTION_REASON_EVENT_TOO_OLD = "EVENT_TOO_OLD"
REJECTION_REASON_NO_RUNTIME_ITEM = "NO_RUNTIME_ITEM"

TERMINAL_NO_MATCH = "NO_MATCHING_WAITING_WORKFLOW"
TERMINAL_NO_READY_DEPLOYMENT = "NO_READY_DEPLOYMENT"
TERMINAL_NO_VERIFIED_PREVIEW = "NO_VERIFIED_PREVIEW"
TERMINAL_ALREADY_RESUMED = "WORKFLOW_ALREADY_RESUMED"
TERMINAL_WORKFLOW_NOT_FOUND = "WORKFLOW_NOT_FOUND"


def log_waiting_for_deployment(
    request: RuntimeValidationRequest,
    *,
    reason: str,
    runtime_validation_id: str | None = None,
    pending_store_size_after_insert: int = 0,
) -> None:
    log_event(
        "WAITING_FOR_DEPLOYMENT",
        workflow_id=request.workflow_id,
        repository=request.repo,
        branch=request.branch,
        commit_sha=_commit_sha(request),
        pull_request=request.pr_number,
        owner=request.requested_by,
        created_at=datetime.now(UTC).isoformat(),
        reason=reason,
        preview_url=request.target_url,
        runtime_validation_id=runtime_validation_id,
        pending_store_size_after_insert=pending_store_size_after_insert,
    )


def log_deployment_status_received(parsed: ParsedGitHubEvent) -> None:
    raw = parsed.raw or {}
    deployment = raw.get("deployment") if isinstance(raw.get("deployment"), dict) else {}
    deployment_status = raw.get("deployment_status") if isinstance(raw.get("deployment_status"), dict) else {}
    log_event(
        "DEPLOYMENT_STATUS_RECEIVED",
        deployment_id=deployment.get("id"),
        deployment_status_id=deployment_status.get("id"),
        repository=parsed.repository,
        branch=deployment.get("ref") or parsed.head_ref,
        sha=deployment.get("sha") or parsed.head_sha,
        environment=deployment_status.get("environment") or deployment.get("environment"),
        state=deployment_status.get("state") or parsed.action,
        target_url=deployment_status.get("target_url") or deployment_status.get("environment_url"),
        created_at=deployment_status.get("created_at") or datetime.now(UTC).isoformat(),
    )


def log_correlation_candidates(
    *,
    waiting_workflows: list[dict[str, Any]] | None = None,
) -> None:
    candidates = waiting_workflows or []
    log_event(
        "WF20_DEPLOYMENT_CORRELATION_CANDIDATES",
        waiting_store_size=len(candidates),
        candidate_workflow_ids=[item.get("workflow_id") for item in candidates],
        candidate_shas=[item.get("commit_sha") for item in candidates],
        candidate_branches=[item.get("branch") for item in candidates],
        candidate_pr_numbers=[item.get("pull_request") or item.get("pr_number") for item in candidates],
    )


def log_correlation_rejection(
    reason: str,
    *,
    workflow_id: str | None = None,
    repository: str | None = None,
    branch: str | None = None,
    commit_sha: str | None = None,
    pull_request: int | None = None,
    deployment_id: Any | None = None,
    deployment_status_id: Any | None = None,
) -> None:
    log_event(
        "WF20_DEPLOYMENT_CORRELATION_REJECTED",
        rejection_reason=reason,
        workflow_id=workflow_id,
        repository=repository,
        branch=branch,
        commit_sha=commit_sha,
        pull_request=pull_request,
        deployment_id=deployment_id,
        deployment_status_id=deployment_status_id,
    )


def log_matched_workflow(
    *,
    workflow_id: str,
    correlation_method: str,
    selected_preview_url: str,
    deployment_id: Any | None = None,
    deployment_status_id: Any | None = None,
) -> None:
    log_event(
        "MATCHED_WORKFLOW",
        workflow_id=workflow_id,
        correlation_method=correlation_method,
        selected_preview_url=selected_preview_url,
        deployment_id=deployment_id,
        deployment_status_id=deployment_status_id,
    )


def log_starting_hermes(
    request: RuntimeValidationRequest,
    *,
    runtime_validation_id: str | None = None,
) -> None:
    log_event(
        "STARTING_HERMES",
        workflow_id=request.workflow_id,
        verified_preview_url=request.target_url,
        runtime_validation_id=runtime_validation_id,
    )


def log_hermes_not_launched(event: str, request: RuntimeValidationRequest | None = None, **fields: Any) -> None:
    log_event(
        event,
        workflow_id=request.workflow_id if request else fields.pop("workflow_id", None),
        repository=request.repo if request else fields.pop("repository", None),
        branch=request.branch if request else fields.pop("branch", None),
        commit_sha=_commit_sha(request) if request else fields.pop("commit_sha", None),
        pull_request=request.pr_number if request else fields.pop("pull_request", None),
        preview_url=request.target_url if request else fields.pop("preview_url", None),
        **fields,
    )


def deployment_ids(parsed: ParsedGitHubEvent) -> tuple[Any | None, Any | None]:
    raw = parsed.raw or {}
    deployment = raw.get("deployment") if isinstance(raw.get("deployment"), dict) else {}
    deployment_status = raw.get("deployment_status") if isinstance(raw.get("deployment_status"), dict) else {}
    return deployment.get("id"), deployment_status.get("id")


def _commit_sha(request: RuntimeValidationRequest | None) -> str | None:
    if request is None:
        return None
    value = getattr(request, "commit_sha", None)
    return str(value) if value else None
