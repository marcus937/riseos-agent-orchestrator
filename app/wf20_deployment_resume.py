from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from app.circuit_runtime_validation import RuntimeValidationRequest
from app.github_events import ParsedGitHubEvent
from app.review_queue import (
    ReviewLifecycleStage,
    ReviewWorkItem,
    ReviewWorkItemStatus,
    record_lifecycle_stage,
    review_queue,
)
from app.wf20_resume_diagnostics import (
    REJECTION_REASON_ALREADY_RESUMED,
    REJECTION_REASON_BRANCH_MISMATCH,
    REJECTION_REASON_PR_MISMATCH,
    REJECTION_REASON_REPOSITORY_MISMATCH,
    REJECTION_REASON_SHA_MISMATCH,
    REJECTION_REASON_STATE_NOT_WAITING,
    TERMINAL_ALREADY_RESUMED,
    TERMINAL_NO_MATCH,
    TERMINAL_NO_READY_DEPLOYMENT,
    deployment_ids,
    log_correlation_candidates,
    log_correlation_rejection,
    log_hermes_not_launched,
    log_matched_workflow,
    log_waiting_for_deployment,
)

WAITING_CONTEXT_SOURCE = "wf20_waiting_for_deployment"
WAITING_RUNTIME_STATUS = "waiting_for_deployment"
RESUMING_RUNTIME_STATUS = "deployment_ready_resuming"
RESUMED_RUNTIME_STATUS = "deployment_ready_resumed"
DEPLOYMENT_FAILED_RUNTIME_STATUS = "deployment_failed"
WAITING_TARGET_SOURCES = {"vercel_timeout", "vercel_preview_pending"}
FAILED_TARGET_SOURCES = {"vercel_failed"}
READY_SOURCE_PREFIX = "github_verified_"

CorrelationMethod = Literal["DEPLOYMENT_ID", "WORKFLOW_ID", "CORRELATION_ID", "SHA", "PR", "BRANCH"]


def is_wf20_deployment_status_payload(parsed: ParsedGitHubEvent) -> bool:
    raw = parsed.raw or {}
    return isinstance(raw.get("deployment"), dict) and isinstance(raw.get("deployment_status"), dict)


def is_waiting_for_deployment_request(request: RuntimeValidationRequest) -> bool:
    return request.target_url is None and str(request.target_url_source or "") in WAITING_TARGET_SOURCES


def is_ready_deployment_request(request: RuntimeValidationRequest) -> bool:
    source = str(request.target_url_source or "")
    return bool(request.target_url) and source.startswith(READY_SOURCE_PREFIX) and "preview_url" in source


def is_failed_deployment_request(request: RuntimeValidationRequest) -> bool:
    return request.target_url is None and str(request.target_url_source or "") in FAILED_TARGET_SOURCES


def persist_waiting_for_deployment(
    request: RuntimeValidationRequest,
    item: ReviewWorkItem,
    *,
    storage: Any | None = None,
) -> ReviewWorkItem:
    commit_sha = _request_commit_sha(request) or item.commit_sha
    item.repo_full_name = request.repo or item.repo_full_name
    item.branch = request.branch or item.branch
    item.base_branch = request.base_branch or item.base_branch
    item.issue_number = request.issue_number or item.issue_number
    item.pr_number = request.pr_number or item.pr_number
    item.commit_sha = commit_sha
    item.status = ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING
    item.runtime_validation_id = request.workflow_id or request.correlation_id
    item.runtime_validation_status = WAITING_RUNTIME_STATUS
    item.runtime_validation_context = _waiting_context(request, item, commit_sha=commit_sha)
    record_lifecycle_stage(item, ReviewLifecycleStage.RUNTIME_VALIDATION_PENDING)
    _save_item(item, storage)
    waiting = list_waiting_deployment_items(storage=storage)
    log_waiting_for_deployment(
        request,
        reason=request.target_url_pending_reason or "Waiting for verified Vercel preview deployment.",
        runtime_validation_id=item.runtime_validation_id,
        pending_store_size_after_insert=len(waiting),
    )
    return item


def claim_waiting_workflow_for_request(
    request: RuntimeValidationRequest,
    parsed: ParsedGitHubEvent,
    *,
    storage: Any | None = None,
) -> ReviewWorkItem | None:
    match = select_waiting_workflow_for_request(request, parsed, storage=storage)
    if match is None:
        log_hermes_not_launched(TERMINAL_NO_MATCH, request)
        return None
    context = dict(match.runtime_validation_context or {})
    if context.get("resume_status") in {"resuming", "resumed"} or match.runtime_validation_status in {RESUMING_RUNTIME_STATUS, RESUMED_RUNTIME_STATUS}:
        log_hermes_not_launched(TERMINAL_ALREADY_RESUMED, request)
        return None
    context["resume_status"] = "resuming"
    context["resumed_at"] = datetime.now(UTC).isoformat()
    context["selected_preview_url"] = request.target_url
    match.runtime_validation_context = context
    match.runtime_validation_status = RESUMING_RUNTIME_STATUS
    _save_item(match, storage)
    return match


def mark_waiting_workflow_resumed(item: ReviewWorkItem, *, storage: Any | None = None) -> ReviewWorkItem:
    context = dict(item.runtime_validation_context or {})
    context["resume_status"] = "resumed"
    context["resumed_at"] = context.get("resumed_at") or datetime.now(UTC).isoformat()
    item.runtime_validation_context = context
    item.runtime_validation_status = RESUMED_RUNTIME_STATUS
    _save_item(item, storage)
    return item


def mark_waiting_workflow_failed_for_request(
    request: RuntimeValidationRequest,
    parsed: ParsedGitHubEvent,
    *,
    storage: Any | None = None,
) -> ReviewWorkItem | None:
    match = select_waiting_workflow_for_request(request, parsed, storage=storage)
    if match is None:
        log_hermes_not_launched(TERMINAL_NO_MATCH, request)
        return None
    context = dict(match.runtime_validation_context or {})
    if context.get("resume_status") in {"resuming", "resumed"} or match.runtime_validation_status in {RESUMING_RUNTIME_STATUS, RESUMED_RUNTIME_STATUS}:
        log_hermes_not_launched(TERMINAL_ALREADY_RESUMED, request)
        return match
    context["resume_status"] = "deployment_failed"
    context["failed_at"] = datetime.now(UTC).isoformat()
    context["failure_reason"] = request.target_url_pending_reason or "Vercel preview deployment failed."
    match.runtime_validation_context = context
    match.runtime_validation_status = DEPLOYMENT_FAILED_RUNTIME_STATUS
    match.status = ReviewWorkItemStatus.BLOCKED
    record_lifecycle_stage(match, ReviewLifecycleStage.RUNTIME_VALIDATION_FAILED, error=str(context["failure_reason"]))
    _save_item(match, storage)
    log_hermes_not_launched(TERMINAL_NO_READY_DEPLOYMENT, request, deployment_failed=True)
    return match


def select_waiting_workflow_for_request(
    request: RuntimeValidationRequest,
    parsed: ParsedGitHubEvent,
    *,
    storage: Any | None = None,
) -> ReviewWorkItem | None:
    waiting_items = list_waiting_deployment_items(storage=storage)
    waiting_workflows = [_candidate_log_payload(item) for item in waiting_items]
    log_correlation_candidates(waiting_workflows=waiting_workflows)
    deployment_id, deployment_status_id = deployment_ids(parsed)
    repository = request.repo

    selectable: list[ReviewWorkItem] = []
    for item in waiting_items:
        context = item.runtime_validation_context or {}
        candidate_workflow_id = _candidate_workflow_id(item)
        candidate_repo = item.repo_full_name or str(context.get("repository") or "") or None
        candidate_branch = item.branch or str(context.get("branch") or "") or None
        candidate_sha = item.commit_sha or str(context.get("commit_sha") or "") or None
        candidate_pr = item.pr_number or _int_or_none(context.get("pr_number"))

        if candidate_repo != repository:
            log_correlation_rejection(
                REJECTION_REASON_REPOSITORY_MISMATCH,
                workflow_id=candidate_workflow_id,
                repository=candidate_repo,
                branch=candidate_branch,
                commit_sha=candidate_sha,
                pull_request=candidate_pr,
                deployment_id=deployment_id,
                deployment_status_id=deployment_status_id,
            )
            continue
        if item.status != ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING or item.runtime_validation_status not in {WAITING_RUNTIME_STATUS, None}:
            log_correlation_rejection(
                REJECTION_REASON_STATE_NOT_WAITING,
                workflow_id=candidate_workflow_id,
                repository=candidate_repo,
                branch=candidate_branch,
                commit_sha=candidate_sha,
                pull_request=candidate_pr,
                deployment_id=deployment_id,
                deployment_status_id=deployment_status_id,
            )
            continue
        if context.get("resume_status") in {"resuming", "resumed"}:
            log_correlation_rejection(
                REJECTION_REASON_ALREADY_RESUMED,
                workflow_id=candidate_workflow_id,
                repository=candidate_repo,
                branch=candidate_branch,
                commit_sha=candidate_sha,
                pull_request=candidate_pr,
                deployment_id=deployment_id,
                deployment_status_id=deployment_status_id,
            )
            continue
        selectable.append(item)

    selected = _select_by(selectable, request, "DEPLOYMENT_ID", parsed=parsed)
    if selected is None:
        selected = _select_by(selectable, request, "WORKFLOW_ID", parsed=parsed)
    if selected is None:
        selected = _select_by(selectable, request, "CORRELATION_ID", parsed=parsed)
    if selected is None:
        selected = _select_by(selectable, request, "SHA", parsed=parsed)
    if selected is None:
        _log_match_rejections(selectable, request, parsed, reason=REJECTION_REASON_SHA_MISMATCH)
        selected = _select_by(selectable, request, "PR", parsed=parsed)
    if selected is None:
        _log_match_rejections(selectable, request, parsed, reason=REJECTION_REASON_PR_MISMATCH)
        selected = _select_by(selectable, request, "BRANCH", parsed=parsed)
    if selected is None:
        _log_match_rejections(selectable, request, parsed, reason=REJECTION_REASON_BRANCH_MISMATCH)
        return None

    method = _selected_method(selected, request, parsed)
    log_matched_workflow(
        workflow_id=_candidate_workflow_id(selected) or request.workflow_id or "unknown",
        correlation_method=method,
        selected_preview_url=request.target_url or "",
        deployment_id=deployment_id,
        deployment_status_id=deployment_status_id,
    )
    return selected


def list_waiting_deployment_items(*, storage: Any | None = None) -> list[ReviewWorkItem]:
    items = storage.list_review_work_items() if storage is not None else review_queue.list_items()
    waiting: list[ReviewWorkItem] = []
    for item in items:
        context = item.runtime_validation_context or {}
        if context.get("source") == WAITING_CONTEXT_SOURCE:
            waiting.append(item)
    return waiting


def _select_by(
    items: list[ReviewWorkItem],
    request: RuntimeValidationRequest,
    method: CorrelationMethod,
    *,
    parsed: ParsedGitHubEvent,
) -> ReviewWorkItem | None:
    for item in items:
        context = item.runtime_validation_context or {}
        if method == "DEPLOYMENT_ID":
            value = _deployment_id_from_parsed(parsed)
            candidate = _context_value(context, "deployment_id")
        elif method == "WORKFLOW_ID":
            value = request.workflow_id
            candidate = _candidate_workflow_id(item)
        elif method == "CORRELATION_ID":
            value = request.correlation_id
            candidate = _context_value(context, "correlation_id")
        elif method == "SHA":
            value = _request_commit_sha(request)
            candidate = item.commit_sha or str(context.get("commit_sha") or "") or None
        elif method == "PR":
            value = request.pr_number
            candidate = item.pr_number or _int_or_none(context.get("pr_number"))
        else:
            value = request.branch
            candidate = item.branch or str(context.get("branch") or "") or None
        if value is not None and candidate == value:
            return item
    return None


def _selected_method(item: ReviewWorkItem, request: RuntimeValidationRequest, parsed: ParsedGitHubEvent) -> CorrelationMethod:
    context = item.runtime_validation_context or {}
    deployment_id = _deployment_id_from_parsed(parsed)
    if deployment_id is not None and _context_value(context, "deployment_id") == deployment_id:
        return "DEPLOYMENT_ID"
    if request.workflow_id is not None and _candidate_workflow_id(item) == request.workflow_id:
        return "WORKFLOW_ID"
    if request.correlation_id is not None and _context_value(context, "correlation_id") == request.correlation_id:
        return "CORRELATION_ID"
    if _request_commit_sha(request) is not None and (item.commit_sha or context.get("commit_sha")) == _request_commit_sha(request):
        return "SHA"
    if request.pr_number is not None and (item.pr_number or _int_or_none(context.get("pr_number"))) == request.pr_number:
        return "PR"
    return "BRANCH"


def _log_match_rejections(items: list[ReviewWorkItem], request: RuntimeValidationRequest, parsed: ParsedGitHubEvent, *, reason: str) -> None:
    deployment_id, deployment_status_id = deployment_ids(parsed)
    for item in items:
        context = item.runtime_validation_context or {}
        log_correlation_rejection(
            reason,
            workflow_id=_candidate_workflow_id(item),
            repository=item.repo_full_name,
            branch=item.branch or str(context.get("branch") or "") or None,
            commit_sha=item.commit_sha or str(context.get("commit_sha") or "") or None,
            pull_request=item.pr_number or _int_or_none(context.get("pr_number")),
            deployment_id=deployment_id,
            deployment_status_id=deployment_status_id,
        )


def _waiting_context(request: RuntimeValidationRequest, item: ReviewWorkItem, *, commit_sha: str | None) -> dict[str, object]:
    now = datetime.now(UTC).isoformat()
    return {
        "source": WAITING_CONTEXT_SOURCE,
        "repository": request.repo,
        "branch": request.branch,
        "base_branch": request.base_branch,
        "pr_number": request.pr_number,
        "pull_request": request.pr_number,
        "commit_sha": commit_sha,
        "workflow_id": request.workflow_id,
        "work_item_id": item.id,
        "runtime_validation_id": item.runtime_validation_id,
        "correlation_id": request.correlation_id,
        "target_url_source": request.target_url_source,
        "target_url_pending_reason": request.target_url_pending_reason,
        "created_at": now,
        "resume_status": "waiting",
    }


def _candidate_log_payload(item: ReviewWorkItem) -> dict[str, Any]:
    context = item.runtime_validation_context or {}
    return {
        "workflow_id": _candidate_workflow_id(item),
        "repository": item.repo_full_name or context.get("repository"),
        "repo": item.repo_full_name or context.get("repository"),
        "branch": item.branch or context.get("branch"),
        "commit_sha": item.commit_sha or context.get("commit_sha"),
        "pr_number": item.pr_number or context.get("pr_number"),
        "pull_request": item.pr_number or context.get("pull_request"),
        "work_item_id": item.id,
        "runtime_validation_id": item.runtime_validation_id or context.get("runtime_validation_id"),
        "runtime_validation_status": item.runtime_validation_status,
    }


def _candidate_workflow_id(item: ReviewWorkItem) -> str | None:
    context = item.runtime_validation_context or {}
    value = context.get("workflow_id") or item.runtime_validation_id
    return str(value) if value else None


def _request_commit_sha(request: RuntimeValidationRequest) -> str | None:
    value = getattr(request, "commit_sha", None)
    return str(value) if value else None


def _deployment_id_from_parsed(parsed: ParsedGitHubEvent) -> str | None:
    value, _status_id = deployment_ids(parsed)
    return value


def _context_value(context: dict[str, Any], key: str) -> str | None:
    value = context.get(key)
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _save_item(item: ReviewWorkItem, storage: Any | None) -> None:
    if storage is not None:
        storage.save_review_work_item(item)


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
