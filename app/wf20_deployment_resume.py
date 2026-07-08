from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from app.circuit_runtime_validation import RuntimeValidationRequest
from app.github_events import ParsedGitHubEvent
from app.operational_logging import log_event
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

_WORKFLOW_CHAIN_CONTEXT_KEYS = {
    "workflow_chain_id",
    "workflow_family",
    "workflow_sequence",
    "workflow_steps",
    "workflow_step",
    "current_workflow_step",
    "previous_workflow_step",
    "next_workflow_step",
    "final_workflow_step",
    "continuation_mode",
    "merge_gate",
    "repository",
    "repo",
    "pr_number",
    "pull_request",
    "branch",
    "base_branch",
    "workflow_id",
    "work_item_id",
    "previous_work_item_id",
    "agent_bus_work_item_id",
    "commit_sha",
}

_WAITING_CONTEXT_AUTHORITATIVE_KEYS = {
    "source",
    "repository",
    "branch",
    "base_branch",
    "pr_number",
    "pull_request",
    "commit_sha",
    "workflow_id",
    "work_item_id",
    "runtime_validation_id",
    "correlation_id",
    "target_url_source",
    "target_url_pending_reason",
    "created_at",
    "resume_status",
}


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
    existing_runtime_context = item.runtime_validation_context if isinstance(item.runtime_validation_context, dict) else {}
    item.repo_full_name = request.repo or item.repo_full_name
    item.branch = request.branch or item.branch
    item.base_branch = request.base_branch or item.base_branch
    item.issue_number = request.issue_number or item.issue_number
    item.pr_number = request.pr_number or item.pr_number
    item.commit_sha = commit_sha
    item.status = ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING
    item.runtime_validation_id = request.workflow_id or request.correlation_id
    item.runtime_validation_status = WAITING_RUNTIME_STATUS
    item.runtime_validation_context = _waiting_context(request, item, commit_sha=commit_sha, existing_context=existing_runtime_context)
    record_lifecycle_stage(item, ReviewLifecycleStage.RUNTIME_VALIDATION_PENDING)
    _log_waiting_registry_transition(
        "registry_before_add",
        request=request,
        item=item,
        storage=storage,
        reason="persist_waiting_for_deployment",
        call_site="persist_waiting_for_deployment",
    )
    _save_item(item, storage)
    waiting = list_waiting_deployment_items(storage=storage)
    _log_waiting_registry_transition(
        "registry_after_add",
        request=request,
        item=item,
        storage=storage,
        matched_workflow_id=_candidate_workflow_id(item),
        matched_review_item=item.id,
        matched_runtime_validation=item.runtime_validation_id,
        reason="persist_waiting_for_deployment",
        call_site="persist_waiting_for_deployment",
    )
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
    _log_waiting_registry_transition(
        "registry_before_remove",
        request=request,
        parsed=parsed,
        item=match,
        storage=storage,
        matched_workflow_id=_candidate_workflow_id(match),
        matched_review_item=match.id,
        matched_runtime_validation=match.runtime_validation_id,
        removed_by="claim_waiting_workflow_for_request",
        reason="deployment_ready_resuming",
        call_site="claim_waiting_workflow_for_request",
    )
    context["resume_status"] = "resuming"
    context["resumed_at"] = datetime.now(UTC).isoformat()
    context["selected_preview_url"] = request.target_url
    match.runtime_validation_context = context
    match.runtime_validation_status = RESUMING_RUNTIME_STATUS
    _save_item(match, storage)
    _log_waiting_registry_transition(
        "registry_after_remove",
        request=request,
        parsed=parsed,
        item=match,
        storage=storage,
        matched_workflow_id=_candidate_workflow_id(match),
        matched_review_item=match.id,
        matched_runtime_validation=match.runtime_validation_id,
        removed_by="claim_waiting_workflow_for_request",
        reason="deployment_ready_resuming",
        call_site="claim_waiting_workflow_for_request",
    )
    return match


def mark_waiting_workflow_resumed(item: ReviewWorkItem, *, storage: Any | None = None) -> ReviewWorkItem:
    context = dict(item.runtime_validation_context or {})
    _log_waiting_registry_transition(
        "registry_before_remove",
        item=item,
        storage=storage,
        matched_workflow_id=_candidate_workflow_id(item),
        matched_review_item=item.id,
        matched_runtime_validation=item.runtime_validation_id,
        removed_by="mark_waiting_workflow_resumed",
        reason="deployment_ready_resumed",
        call_site="mark_waiting_workflow_resumed",
    )
    context["resume_status"] = "resumed"
    context["resumed_at"] = context.get("resumed_at") or datetime.now(UTC).isoformat()
    item.runtime_validation_context = context
    item.runtime_validation_status = RESUMED_RUNTIME_STATUS
    _save_item(item, storage)
    _log_waiting_registry_transition(
        "registry_after_remove",
        item=item,
        storage=storage,
        matched_workflow_id=_candidate_workflow_id(item),
        matched_review_item=item.id,
        matched_runtime_validation=item.runtime_validation_id,
        removed_by="mark_waiting_workflow_resumed",
        reason="deployment_ready_resumed",
        call_site="mark_waiting_workflow_resumed",
    )
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
    _log_waiting_registry_transition(
        "registry_before_remove",
        request=request,
        parsed=parsed,
        item=match,
        storage=storage,
        matched_workflow_id=_candidate_workflow_id(match),
        matched_review_item=match.id,
        matched_runtime_validation=match.runtime_validation_id,
        removed_by="mark_waiting_workflow_failed_for_request",
        reason="deployment_failed",
        call_site="mark_waiting_workflow_failed_for_request",
    )
    context["resume_status"] = "deployment_failed"
    context["failed_at"] = datetime.now(UTC).isoformat()
    context["failure_reason"] = request.target_url_pending_reason or "Vercel preview deployment failed."
    match.runtime_validation_context = context
    match.runtime_validation_status = DEPLOYMENT_FAILED_RUNTIME_STATUS
    match.status = ReviewWorkItemStatus.BLOCKED
    record_lifecycle_stage(match, ReviewLifecycleStage.RUNTIME_VALIDATION_FAILED, error=str(context["failure_reason"]))
    _save_item(match, storage)
    _log_waiting_registry_transition(
        "registry_after_remove",
        request=request,
        parsed=parsed,
        item=match,
        storage=storage,
        matched_workflow_id=_candidate_workflow_id(match),
        matched_review_item=match.id,
        matched_runtime_validation=match.runtime_validation_id,
        removed_by="mark_waiting_workflow_failed_for_request",
        reason="deployment_failed",
        call_site="mark_waiting_workflow_failed_for_request",
    )
    log_hermes_not_launched(TERMINAL_NO_READY_DEPLOYMENT, request, deployment_failed=True)
    return match


def select_waiting_workflow_for_request(
    request: RuntimeValidationRequest,
    parsed: ParsedGitHubEvent,
    *,
    storage: Any | None = None,
) -> ReviewWorkItem | None:
    _log_waiting_registry_transition(
        "registry_before_lookup",
        request=request,
        parsed=parsed,
        storage=storage,
        correlation_lookup_key=_lookup_key(request, parsed, "START"),
        reason="select_waiting_workflow_for_request",
        call_site="select_waiting_workflow_for_request",
    )
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
        _log_waiting_registry_transition(
            "registry_after_lookup",
            request=request,
            parsed=parsed,
            storage=storage,
            correlation_lookup_key=_lookup_key(request, parsed, "NO_MATCH"),
            reason="NO_MATCHING_WAITING_WORKFLOW",
            call_site="select_waiting_workflow_for_request",
        )
        return None

    method = _selected_method(selected, request, parsed)
    _log_waiting_registry_transition(
        "registry_after_lookup",
        request=request,
        parsed=parsed,
        item=selected,
        storage=storage,
        correlation_lookup_key=_lookup_key(request, parsed, method),
        matched_workflow_id=_candidate_workflow_id(selected),
        matched_review_item=selected.id,
        matched_runtime_validation=selected.runtime_validation_id,
        reason="MATCHED_WAITING_WORKFLOW",
        call_site="select_waiting_workflow_for_request",
    )
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
    log_event(
        "registry_lookup_method_started",
        **_registry_log_fields(
            request=request,
            parsed=parsed,
            waiting_items=items,
            correlation_lookup_key=_lookup_key(request, parsed, method),
            reason=f"lookup_by_{method}",
            call_site="_select_by",
        ),
    )
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
            log_event(
                "registry_lookup_method_matched",
                **_registry_log_fields(
                    request=request,
                    parsed=parsed,
                    item=item,
                    waiting_items=items,
                    correlation_lookup_key=_lookup_key(request, parsed, method),
                    matched_workflow_id=_candidate_workflow_id(item),
                    matched_review_item=item.id,
                    matched_runtime_validation=item.runtime_validation_id,
                    reason=f"lookup_by_{method}",
                    call_site="_select_by",
                ),
            )
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


def _waiting_context(
    request: RuntimeValidationRequest,
    item: ReviewWorkItem,
    *,
    commit_sha: str | None,
    existing_context: dict[str, Any] | None = None,
) -> dict[str, object]:
    now = datetime.now(UTC).isoformat()
    context: dict[str, object] = {
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
    _preserve_waiting_workflow_context(context, existing_context or {})
    return context


def _preserve_waiting_workflow_context(context: dict[str, object], existing_context: dict[str, Any]) -> None:
    if not isinstance(existing_context, dict) or not existing_context:
        return
    for key, value in existing_context.items():
        if key in _WAITING_CONTEXT_AUTHORITATIVE_KEYS:
            continue
        if _value_present(value) and key not in context:
            context[key] = value

    existing_dispatch = _dict_value(existing_context.get("review_dispatch"))
    merged_dispatch = dict(_dict_value(context.get("review_dispatch")))
    for key, value in existing_dispatch.items():
        if _value_present(value) and not _value_present(merged_dispatch.get(key)):
            merged_dispatch[key] = value

    workflow_chain = _workflow_chain_from_context(existing_context, existing_dispatch)
    metadata = _dict_value(existing_context.get("metadata"))
    if workflow_chain:
        context.setdefault("workflow_chain", workflow_chain)
        merged_dispatch.setdefault("workflow_chain", workflow_chain)
        metadata.setdefault("workflow_chain", workflow_chain)

    for key in _WORKFLOW_CHAIN_CONTEXT_KEYS:
        value = _first_present(
            existing_context.get(key),
            existing_dispatch.get(key),
            metadata.get(key),
            workflow_chain.get(key) if workflow_chain else None,
        )
        if _value_present(value):
            if key not in _WAITING_CONTEXT_AUTHORITATIVE_KEYS:
                context.setdefault(key, value)
            merged_dispatch.setdefault(key, value)
            metadata.setdefault(key, value)

    if merged_dispatch:
        context["review_dispatch"] = merged_dispatch
    if metadata:
        context["metadata"] = metadata


def _workflow_chain_from_context(context: dict[str, Any], review_dispatch: dict[str, Any]) -> dict[str, Any]:
    metadata = _dict_value(context.get("metadata"))
    return _first_dict(
        context.get("workflow_chain"),
        context.get("_workflow_chain"),
        context.get("workflowChain"),
        review_dispatch.get("workflow_chain"),
        review_dispatch.get("_workflow_chain"),
        review_dispatch.get("workflowChain"),
        metadata.get("workflow_chain"),
        metadata.get("_workflow_chain"),
        metadata.get("workflowChain"),
    )


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


def _log_waiting_registry_transition(
    event: str,
    *,
    request: RuntimeValidationRequest | None = None,
    parsed: ParsedGitHubEvent | None = None,
    item: ReviewWorkItem | None = None,
    storage: Any | None = None,
    correlation_lookup_key: str | None = None,
    matched_workflow_id: str | None = None,
    matched_review_item: str | None = None,
    matched_runtime_validation: str | None = None,
    removed_by: str | None = None,
    reason: str | None = None,
    call_site: str | None = None,
) -> None:
    log_event(
        event,
        **_registry_log_fields(
            request=request,
            parsed=parsed,
            item=item,
            waiting_items=list_waiting_deployment_items(storage=storage),
            correlation_lookup_key=correlation_lookup_key,
            matched_workflow_id=matched_workflow_id,
            matched_review_item=matched_review_item,
            matched_runtime_validation=matched_runtime_validation,
            removed_by=removed_by,
            reason=reason,
            call_site=call_site,
        ),
    )


def _registry_log_fields(
    *,
    request: RuntimeValidationRequest | None = None,
    parsed: ParsedGitHubEvent | None = None,
    item: ReviewWorkItem | None = None,
    waiting_items: list[ReviewWorkItem] | None = None,
    correlation_lookup_key: str | None = None,
    matched_workflow_id: str | None = None,
    matched_review_item: str | None = None,
    matched_runtime_validation: str | None = None,
    removed_by: str | None = None,
    reason: str | None = None,
    call_site: str | None = None,
) -> dict[str, Any]:
    waiting_items = waiting_items or []
    deployment = _deployment_from_parsed(parsed)
    context = item.runtime_validation_context if item is not None and isinstance(item.runtime_validation_context, dict) else {}
    return {
        "workflow_id": _first_text(
            getattr(request, "workflow_id", None),
            context.get("workflow_id"),
            _candidate_workflow_id(item) if item is not None else None,
        ),
        "review_item_id": item.id if item is not None else None,
        "work_item_id": _first_text(
            getattr(request, "work_item_id", None),
            context.get("work_item_id"),
            getattr(item, "agent_bus_work_item_id", None) if item is not None else None,
            item.id if item is not None else None,
        ),
        "runtime_validation_id": _first_text(
            getattr(request, "workflow_id", None),
            getattr(item, "runtime_validation_id", None) if item is not None else None,
            context.get("runtime_validation_id"),
        ),
        "repository": _first_text(
            getattr(request, "repo", None),
            getattr(item, "repo_full_name", None) if item is not None else None,
            context.get("repository"),
        ),
        "pr_number": _first_present(
            getattr(request, "pr_number", None),
            getattr(item, "pr_number", None) if item is not None else None,
            context.get("pr_number"),
        ),
        "branch": _first_text(
            getattr(request, "branch", None),
            getattr(item, "branch", None) if item is not None else None,
            context.get("branch"),
        ),
        "base_branch": _first_text(
            getattr(request, "base_branch", None),
            getattr(item, "base_branch", None) if item is not None else None,
            context.get("base_branch"),
        ),
        "commit_sha": _first_text(
            _request_commit_sha(request) if request is not None else None,
            getattr(item, "commit_sha", None) if item is not None else None,
            context.get("commit_sha"),
        ),
        "deployment_sha": deployment.get("sha"),
        "deployment_branch": deployment.get("ref"),
        "deployment_repository": parsed.repository if parsed is not None else None,
        "waiting_registry_size": len(waiting_items),
        "waiting_registry_keys": [_waiting_registry_key(waiting_item) for waiting_item in waiting_items],
        "correlation_lookup_key": correlation_lookup_key,
        "matched_workflow_id": matched_workflow_id,
        "matched_review_item": matched_review_item,
        "matched_runtime_validation": matched_runtime_validation,
        "removed_by": removed_by,
        "reason": reason,
        "call_site": call_site,
        "_include_nulls": True,
    }


def _lookup_key(request: RuntimeValidationRequest, parsed: ParsedGitHubEvent, method: str) -> str:
    deployment_id = _deployment_id_from_parsed(parsed)
    values = {
        "deployment_id": deployment_id,
        "workflow_id": request.workflow_id,
        "correlation_id": request.correlation_id,
        "sha": _request_commit_sha(request),
        "pr": request.pr_number,
        "branch": request.branch,
    }
    compact = ",".join(f"{key}={value}" for key, value in values.items() if value not in (None, ""))
    return f"{method}:{compact}"


def _waiting_registry_key(item: ReviewWorkItem) -> str:
    context = item.runtime_validation_context or {}
    repo = item.repo_full_name or context.get("repository") or "unknown-repo"
    pr_number = item.pr_number or context.get("pr_number") or "unknown-pr"
    branch = item.branch or context.get("branch") or "unknown-branch"
    sha = item.commit_sha or context.get("commit_sha") or "unknown-sha"
    workflow_id = _candidate_workflow_id(item) or "unknown-workflow"
    status = item.runtime_validation_status or context.get("resume_status") or str(item.status)
    return f"{repo}#PR{pr_number}:{branch}:{sha}:{workflow_id}:{status}"


def _deployment_from_parsed(parsed: ParsedGitHubEvent | None) -> dict[str, Any]:
    if parsed is None:
        return {}
    raw = parsed.raw or {}
    deployment = raw.get("deployment") if isinstance(raw.get("deployment"), dict) else {}
    return deployment


def _first_text(*values: Any) -> str | None:
    value = _first_present(*values)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_present(*values: Any) -> Any | None:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict) and value:
            return value
    return {}


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _value_present(value: Any) -> bool:
    return value is not None and value != ""


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
