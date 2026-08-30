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
from app.workflow_chain_diagnostics import log_workflow_chain_availability

TERMINAL_RUNTIME_VALIDATION_STATUSES = {"blocked", "completed", "failed"}
RUNTIME_REVIEW_SOURCE = "runtime_validation_bb2_packet"
IMMUTABLE_CORRELATION_KEYS = ("deployment_id", "workflow_id", "correlation_id")
_WORKFLOW_CHAIN_KEYS = {
    "workflow_chain_id",
    "workflow_family",
    "workflow_sequence",
    "workflow_steps",
    "workflow_step",
    "current_workflow_step",
    "next_workflow_step",
    "final_workflow_step",
    "continuation_mode",
    "merge_gate",
}
_WORKFLOW_COMPANION_KEYS = {
    *_WORKFLOW_CHAIN_KEYS,
    "workflow_id",
    "repository",
    "repo",
    "pr_number",
    "branch",
    "base_branch",
    "commit_sha",
    "work_item_id",
    "previous_work_item_id",
    "agent_bus_work_item_id",
}


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
    agent_task_store: Any | None = None,
    agent_bus_work_item: dict[str, Any] | None = None,
) -> ReviewWorkItem | None:
    if not settings.enable_runtime_validation_review_bridge:
        return None
    if result.status not in TERMINAL_RUNTIME_VALIDATION_STATUSES:
        return None

    digest = stable_validation_digest(result)
    duplicate = _find_exact_runtime_result(result, digest=digest, storage=storage)
    if duplicate is not None:
        log_workflow_chain_availability(
            "wf_chain_metadata_runtime_bridge_duplicate_result",
            duplicate,
        )
        result.bb2.packet_created = True
        result.bb2.review_requested = True
        return duplicate

    item = existing_item or _find_pending_runtime_result(result, storage=storage)
    if item is None:
        item = _review_work_item_from_runtime_validation(
            result,
            agent_task_store=agent_task_store,
            agent_bus_work_item=agent_bus_work_item,
        )
    else:
        _log_existing_review_work_item_constructor_bypassed(item, result)
    _attach_runtime_validation_context(item, result, digest=digest)
    log_workflow_chain_availability(
        "wf_chain_metadata_runtime_bridge_after_context_attach",
        item,
    )
    _normalize_workflow_chain_metadata(
        item.runtime_validation_context,
        result,
        item=item,
        agent_task_store=agent_task_store,
        agent_bus_work_item=agent_bus_work_item,
    )
    log_workflow_chain_availability(
        "wf_chain_metadata_runtime_bridge_after_metadata_normalize",
        item,
    )
    _log_review_item_chain_hydrated(item, result)
    before_result_dispatch = _dict_value(result.review_dispatch)
    assigned_dispatch = _dict_value(item.runtime_validation_context.get("review_dispatch"))
    _log_workflow_chain_assignment(
        event="workflow_chain_assignment_before",
        assignment="result.review_dispatch",
        source_object=item.runtime_validation_context,
        source_object_label="ReviewWorkItem.runtime_validation_context",
        target_object=result,
        before_value=before_result_dispatch,
        after_value=assigned_dispatch,
        runtime_validation_id=result.validation_id,
        workflow_id=result.workflow_id,
        work_item_id=result.work_item_id,
    )
    result.review_dispatch = assigned_dispatch
    _log_workflow_chain_assignment(
        event="workflow_chain_assignment_after",
        assignment="result.review_dispatch",
        source_object=item.runtime_validation_context,
        source_object_label="ReviewWorkItem.runtime_validation_context",
        target_object=result,
        before_value=before_result_dispatch,
        after_value=result.review_dispatch,
        runtime_validation_id=result.validation_id,
        workflow_id=result.workflow_id,
        work_item_id=result.work_item_id,
    )

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
        log_workflow_chain_availability(
            "wf_chain_metadata_runtime_bridge_before_persist",
            item,
            persistence="sqlite",
        )
        storage.save_review_work_item(item)
        return item
    log_workflow_chain_availability(
        "wf_chain_metadata_runtime_bridge_before_queue_add",
        item,
        persistence="memory",
    )
    queued = review_queue.add_if_absent(item)
    return queued


def runtime_validation_context_from_result(result: RuntimeValidationResult) -> dict[str, object]:
    evidence = result.evidence.model_dump(mode="json")
    hermes = result.hermes.model_dump(mode="json")
    bb2 = result.bb2.model_dump(mode="json")
    context = {
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
    _log_workflow_chain_assignment(
        event="workflow_chain_assignment_after",
        assignment="runtime_context.review_dispatch",
        source_object=result,
        source_object_label="RuntimeValidationResult",
        target_object=context,
        before_value=None,
        after_value=context.get("review_dispatch"),
        runtime_validation_id=result.validation_id,
        workflow_id=result.workflow_id,
        work_item_id=result.work_item_id,
    )
    return context


def _review_work_item_from_runtime_validation(
    result: RuntimeValidationResult,
    *,
    agent_task_store: Any | None = None,
    agent_bus_work_item: dict[str, Any] | None = None,
) -> ReviewWorkItem:
    now = datetime.now(UTC)
    runtime_context = runtime_validation_context_from_result(result)
    _hydrate_runtime_context_from_agent_bus_work_item(runtime_context, agent_bus_work_item)
    _normalize_workflow_chain_metadata(
        runtime_context,
        result,
        item=None,
        agent_task_store=agent_task_store,
        agent_bus_work_item=agent_bus_work_item,
    )
    _log_wf_chain_hydrated(runtime_context)
    constructor_args = {
        "id": str(uuid4()),
        "created_at": now,
        "updated_at": now,
        "repo_full_name": result.repo,
        "event_type": GitHubEventType.PULL_REQUEST if result.pr_number is not None else GitHubEventType.ISSUES,
        "branch": result.branch,
        "base_branch": result.base_branch,
        "commit_sha": _commit_sha_from_result(result),
        "issue_number": result.issue_number,
        "pr_number": result.pr_number,
        "labels": ["bb-review-needed", "runtime-agent"],
        "agent_bus_work_item_id": result.work_item_id,
        "runtime_validation_id": result.validation_id,
        "runtime_validation_status": result.status,
        "runtime_validation_completed_at": result.completed_at,
        "runtime_validation_context": runtime_context,
    }
    _log_review_work_item_constructor_input(
        result=result,
        runtime_context=runtime_context,
        constructor_args=constructor_args,
        source_object="RuntimeValidationResult",
    )
    return ReviewWorkItem(**constructor_args)


def _hydrate_runtime_context_from_agent_bus_work_item(
    context: dict[str, Any],
    agent_bus_work_item: dict[str, Any] | None,
) -> None:
    sources = _agent_bus_work_item_sources(agent_bus_work_item)
    if not sources:
        _log_workflow_chain_missing_source(
            source_object=agent_bus_work_item,
            source_object_label="agent_bus_work_item",
            reason="agent_bus_work_item_sources_empty",
        )
        return

    review_dispatch = dict(_dict_value(context.get("review_dispatch")))
    metadata = dict(_dict_value(context.get("metadata")))
    for source in sources:
        source_metadata = _dict_value(source.get("metadata"))
        if source_metadata:
            for key, value in source_metadata.items():
                if _value_present(value) and key not in metadata:
                    metadata[key] = value
        source_dispatch = _dict_value(source.get("review_dispatch")) or _dict_value(source_metadata.get("review_dispatch"))
        for key, value in source_dispatch.items():
            if _value_present(value) and not _value_present(review_dispatch.get(key)):
                review_dispatch[key] = value

    workflow_chain = _workflow_chain_object(review_dispatch, sources)
    if workflow_chain:
        before_context = context.get("workflow_chain")
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_before",
            assignment="runtime_context.workflow_chain",
            source_object=sources,
            source_object_label="agent_bus_work_item_sources",
            target_object=context,
            before_value=before_context,
            after_value=workflow_chain,
        )
        context["workflow_chain"] = workflow_chain
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_after",
            assignment="runtime_context.workflow_chain",
            source_object=sources,
            source_object_label="agent_bus_work_item_sources",
            target_object=context,
            before_value=before_context,
            after_value=context.get("workflow_chain"),
        )
        before_dispatch = review_dispatch.get("workflow_chain")
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_before",
            assignment="review_dispatch.workflow_chain",
            source_object=context,
            source_object_label="runtime_context",
            target_object=review_dispatch,
            before_value=before_dispatch,
            after_value=workflow_chain,
        )
        review_dispatch.setdefault("workflow_chain", workflow_chain)
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_after",
            assignment="review_dispatch.workflow_chain",
            source_object=context,
            source_object_label="runtime_context",
            target_object=review_dispatch,
            before_value=before_dispatch,
            after_value=review_dispatch.get("workflow_chain"),
        )
        before_metadata = metadata.get("workflow_chain")
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_before",
            assignment="metadata.workflow_chain",
            source_object=review_dispatch,
            source_object_label="review_dispatch",
            target_object=metadata,
            before_value=before_metadata,
            after_value=workflow_chain,
        )
        metadata.setdefault("workflow_chain", workflow_chain)
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_after",
            assignment="metadata.workflow_chain",
            source_object=review_dispatch,
            source_object_label="review_dispatch",
            target_object=metadata,
            before_value=before_metadata,
            after_value=metadata.get("workflow_chain"),
        )
    else:
        _log_workflow_chain_missing_source(
            source_object=sources,
            source_object_label="agent_bus_work_item_sources",
            reason="workflow_chain_not_found",
        )

    for key in _WORKFLOW_COMPANION_KEYS:
        value = _first_present_from_sources(sources, key, _camelize(key))
        if _value_present(value):
            context.setdefault(key, value)
            review_dispatch.setdefault(key, value)
            metadata.setdefault(key, value)

    if review_dispatch:
        context["review_dispatch"] = review_dispatch
    if metadata:
        context["metadata"] = metadata


def _log_wf_chain_hydrated(context: dict[str, Any]) -> None:
    workflow_chain = _dict_value(context.get("workflow_chain"))
    log_event(
        "WF_CHAIN_HYDRATED",
        workflow_chain_present=bool(workflow_chain),
        workflow_chain_length=len(workflow_chain or []),
        workflow_chain_id=context.get("workflow_chain_id"),
        current_workflow_step=context.get("current_workflow_step"),
        next_workflow_step=context.get("next_workflow_step"),
        runtime_validation_id=context.get("runtime_validation_id") or context.get("validation_id"),
        work_item_id=context.get("work_item_id"),
        correlation_id=context.get("correlation_id"),
    )


def _log_review_work_item_constructor_input(
    *,
    result: RuntimeValidationResult,
    runtime_context: dict[str, Any],
    constructor_args: dict[str, Any],
    source_object: str,
) -> None:
    review_dispatch = _dict_value(runtime_context.get("review_dispatch"))
    metadata = _dict_value(runtime_context.get("metadata"))
    bb2_context = result.bb2.review_context if isinstance(result.bb2.review_context, dict) else {}
    workflow_chain = _workflow_chain_object(review_dispatch, [runtime_context, review_dispatch, metadata, bb2_context])
    log_event(
        "review_work_item_constructor_input",
        source_object=source_object,
        source_object_type=type(result).__name__,
        source_object_module=type(result).__module__,
        runtime_validation_id=result.validation_id,
        workflow_id=runtime_context.get("workflow_id") or result.workflow_id,
        work_item_id=runtime_context.get("work_item_id") or result.work_item_id,
        repository=result.repo,
        pr_number=result.pr_number,
        branch=result.branch,
        constructor_arg_keys=sorted(str(key) for key in constructor_args.keys()),
        constructor_runtime_context_keys=sorted(str(key) for key in _dict_value(constructor_args.get("runtime_validation_context")).keys()),
        review_dispatch_keys=sorted(str(key) for key in review_dispatch.keys()),
        runtime_context_keys=sorted(str(key) for key in runtime_context.keys()),
        metadata_keys=sorted(str(key) for key in metadata.keys()),
        bb2_review_context_keys=sorted(str(key) for key in bb2_context.keys()),
        workflow_chain=workflow_chain or None,
        workflow_chain_populated=bool(workflow_chain),
        workflow_chain_length=len(workflow_chain),
        current_workflow_step=_first_present_from_sources([workflow_chain, runtime_context, review_dispatch, metadata, bb2_context], "current_workflow_step", "currentWorkflowStep", "workflow_step", "workflowStep"),
        next_workflow_step=_first_present_from_sources([workflow_chain, runtime_context, review_dispatch, metadata, bb2_context], "next_workflow_step", "nextWorkflowStep"),
        source_review_dispatch_keys=sorted(str(key) for key in _dict_value(result.review_dispatch).keys()),
        source_bb2_packet_keys=sorted(str(key) for key in result.bb2.model_dump(mode="json").keys()),
        _include_nulls=True,
    )


def _log_existing_review_work_item_constructor_bypassed(item: ReviewWorkItem, result: RuntimeValidationResult) -> None:
    context = item.runtime_validation_context if isinstance(item.runtime_validation_context, dict) else {}
    review_dispatch = _dict_value(context.get("review_dispatch"))
    metadata = _dict_value(context.get("metadata"))
    workflow_chain = _workflow_chain_object(review_dispatch, [context, review_dispatch, metadata])
    log_event(
        "review_work_item_constructor_bypassed_existing_item",
        source_object="existing ReviewWorkItem",
        source_object_type=type(item).__name__,
        runtime_validation_id=result.validation_id,
        review_item_id=item.id,
        workflow_id=context.get("workflow_id") or result.workflow_id,
        work_item_id=context.get("work_item_id") or result.work_item_id,
        repository=item.repo_full_name or result.repo,
        pr_number=item.pr_number or result.pr_number,
        branch=item.branch or result.branch,
        review_dispatch_keys=sorted(str(key) for key in review_dispatch.keys()),
        runtime_context_keys=sorted(str(key) for key in context.keys()),
        metadata_keys=sorted(str(key) for key in metadata.keys()),
        workflow_chain=workflow_chain or None,
        workflow_chain_populated=bool(workflow_chain),
        workflow_chain_length=len(workflow_chain),
        current_workflow_step=_first_present_from_sources([workflow_chain, context, review_dispatch, metadata], "current_workflow_step", "currentWorkflowStep", "workflow_step", "workflowStep"),
        next_workflow_step=_first_present_from_sources([workflow_chain, context, review_dispatch, metadata], "next_workflow_step", "nextWorkflowStep"),
        _include_nulls=True,
    )


def _log_workflow_chain_assignment(
    *,
    event: str,
    assignment: str,
    source_object: Any,
    source_object_label: str,
    target_object: Any,
    before_value: Any,
    after_value: Any,
    runtime_validation_id: str | None = None,
    workflow_id: str | None = None,
    work_item_id: str | None = None,
) -> None:
    before_chain = _chain_or_self(before_value)
    after_chain = _chain_or_self(after_value)
    source_chain = _chain_from_any(source_object)
    log_event(
        event,
        assignment=assignment,
        source_object=source_object_label,
        source_object_type=type(source_object).__name__,
        target_object_type=type(target_object).__name__,
        runtime_validation_id=runtime_validation_id,
        workflow_id=workflow_id,
        work_item_id=work_item_id,
        source_workflow_chain_present=bool(source_chain),
        source_workflow_chain=source_chain or None,
        before_workflow_chain_present=bool(before_chain),
        before_workflow_chain=before_chain or None,
        after_workflow_chain_present=bool(after_chain),
        after_workflow_chain=after_chain or None,
        current_workflow_step=_first_present_from_sources([after_chain, before_chain, source_chain], "current_workflow_step", "currentWorkflowStep", "workflow_step", "workflowStep"),
        next_workflow_step=_first_present_from_sources([after_chain, before_chain, source_chain], "next_workflow_step", "nextWorkflowStep"),
        _include_nulls=True,
    )
    if not source_chain:
        _log_workflow_chain_missing_source(
            source_object=source_object,
            source_object_label=source_object_label,
            reason=f"source_lacks_workflow_chain_for_{assignment}",
            runtime_validation_id=runtime_validation_id,
            workflow_id=workflow_id,
            work_item_id=work_item_id,
        )


def _log_workflow_chain_missing_source(
    *,
    source_object: Any,
    source_object_label: str,
    reason: str,
    runtime_validation_id: str | None = None,
    workflow_id: str | None = None,
    work_item_id: str | None = None,
) -> None:
    log_event(
        "workflow_chain_source_missing",
        source_object=source_object_label,
        source_object_type=type(source_object).__name__,
        reason=reason,
        runtime_validation_id=runtime_validation_id,
        workflow_id=workflow_id,
        work_item_id=work_item_id,
        _include_nulls=True,
    )


def _chain_or_self(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and any(key in value for key in _WORKFLOW_CHAIN_KEYS | {"workflow_chain", "_workflow_chain", "workflowChain"}):
        return _chain_from_any(value) or value
    return _chain_from_any(value)


def _chain_from_any(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        for entry in value:
            chain = _chain_from_any(entry)
            if chain:
                return chain
        return {}
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json")
        except Exception:
            return {}
    if not isinstance(value, dict):
        return {}
    for key in ("workflow_chain", "_workflow_chain", "workflowChain"):
        nested = value.get(key)
        if isinstance(nested, dict) and nested:
            return nested
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        chain = _chain_from_any(metadata)
        if chain:
            return chain
    review_dispatch = value.get("review_dispatch") or value.get("reviewDispatch")
    if isinstance(review_dispatch, dict):
        chain = _chain_from_any(review_dispatch)
        if chain:
            return chain
    runtime_context = value.get("runtime_context") or value.get("runtimeContext") or value.get("runtime_validation_context") or value.get("runtimeValidationContext")
    if isinstance(runtime_context, dict):
        chain = _chain_from_any(runtime_context)
        if chain:
            return chain
    if any(key in value for key in _WORKFLOW_CHAIN_KEYS):
        return {key: value[key] for key in _WORKFLOW_CHAIN_KEYS if key in value and _value_present(value.get(key))}
    return {}


def _log_review_item_chain_hydrated(item: ReviewWorkItem, result: RuntimeValidationResult) -> None:
    context = item.runtime_validation_context if isinstance(item.runtime_validation_context, dict) else {}
    review_dispatch = _dict_value(context.get("review_dispatch"))
    workflow_chain = _workflow_chain_object(review_dispatch, [context, review_dispatch])
    log_event(
        "runtime_validation_review_item_workflow_chain_hydrated",
        review_item_id=item.id,
        runtime_validation_id=result.validation_id,
        workflow_id=context.get("workflow_id") or result.workflow_id,
        work_item_id=context.get("work_item_id") or result.work_item_id,
        repository=item.repo_full_name or result.repo,
        pr_number=item.pr_number or result.pr_number,
        branch=item.branch or result.branch,
        workflow_chain_present=bool(workflow_chain),
        workflow_chain_length=len(workflow_chain),
        workflow_chain_keys=sorted(str(key) for key in workflow_chain.keys()),
        review_dispatch_workflow_chain_present=bool(_dict_value(review_dispatch.get("workflow_chain"))),
        runtime_context_workflow_chain_present=bool(_dict_value(context.get("workflow_chain"))),
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
    existing_context = item.runtime_validation_context if isinstance(item.runtime_validation_context, dict) else {}
    item.runtime_validation_context = _merge_runtime_validation_context(
        runtime_validation_context_from_result(result),
        existing_context,
    )


def _merge_runtime_validation_context(base: dict[str, object], hydrated: dict[str, object]) -> dict[str, object]:
    if not hydrated:
        return base
    merged = dict(base)
    for key in _WORKFLOW_COMPANION_KEYS:
        value = hydrated.get(key)
        if _value_present(value) and not _value_present(merged.get(key)):
            merged[key] = value
    for key in ("workflow_chain", "_workflow_chain"):
        value = hydrated.get(key)
        if isinstance(value, dict) and value:
            before_merged = merged.get(key)
            _log_workflow_chain_assignment(
                event="workflow_chain_assignment_before",
                assignment=f"merged.{key}",
                source_object=hydrated,
                source_object_label="hydrated runtime context",
                target_object=merged,
                before_value=before_merged,
                after_value=value,
            )
            merged.setdefault(key, value)
            _log_workflow_chain_assignment(
                event="workflow_chain_assignment_after",
                assignment=f"merged.{key}",
                source_object=hydrated,
                source_object_label="hydrated runtime context",
                target_object=merged,
                before_value=before_merged,
                after_value=merged.get(key),
            )

    base_metadata = dict(_dict_value(base.get("metadata")))
    hydrated_metadata = _dict_value(hydrated.get("metadata"))
    for key, value in hydrated_metadata.items():
        if _value_present(value) and not _value_present(base_metadata.get(key)):
            base_metadata[key] = value
    workflow_chain = _dict_value(merged.get("workflow_chain")) or _dict_value(merged.get("_workflow_chain"))
    if workflow_chain:
        before_metadata = base_metadata.get("workflow_chain")
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_before",
            assignment="metadata.workflow_chain",
            source_object=merged,
            source_object_label="merged runtime context",
            target_object=base_metadata,
            before_value=before_metadata,
            after_value=workflow_chain,
        )
        base_metadata.setdefault("workflow_chain", workflow_chain)
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_after",
            assignment="metadata.workflow_chain",
            source_object=merged,
            source_object_label="merged runtime context",
            target_object=base_metadata,
            before_value=before_metadata,
            after_value=base_metadata.get("workflow_chain"),
        )

    base_dispatch = dict(_dict_value(base.get("review_dispatch")))
    hydrated_dispatch = _dict_value(hydrated.get("review_dispatch"))
    for key, value in hydrated_dispatch.items():
        if _value_present(value) and not _value_present(base_dispatch.get(key)):
            base_dispatch[key] = value
    for key in ("workflow_chain", "_workflow_chain"):
        value = hydrated_dispatch.get(key)
        if isinstance(value, dict) and value:
            before_dispatch = base_dispatch.get(key)
            _log_workflow_chain_assignment(
                event="workflow_chain_assignment_before",
                assignment=f"review_dispatch.{key}",
                source_object=hydrated_dispatch,
                source_object_label="hydrated review_dispatch",
                target_object=base_dispatch,
                before_value=before_dispatch,
                after_value=value,
            )
            base_dispatch.setdefault(key, value)
            base_metadata.setdefault("workflow_chain", value)
            _log_workflow_chain_assignment(
                event="workflow_chain_assignment_after",
                assignment=f"review_dispatch.{key}",
                source_object=hydrated_dispatch,
                source_object_label="hydrated review_dispatch",
                target_object=base_dispatch,
                before_value=before_dispatch,
                after_value=base_dispatch.get(key),
            )
    if base_dispatch:
        merged["review_dispatch"] = base_dispatch
    if base_metadata:
        merged["metadata"] = base_metadata
    return merged


def _normalize_workflow_chain_metadata(
    context: dict[str, Any],
    result: RuntimeValidationResult,
    *,
    item: ReviewWorkItem | None,
    agent_task_store: Any | None,
    agent_bus_work_item: dict[str, Any] | None,
) -> None:
    review_dispatch = dict(_dict_value(context.get("review_dispatch")))
    sources = [review_dispatch, context]
    sources.extend(_runtime_result_sources(result))
    sources.extend(_agent_bus_work_item_sources(agent_bus_work_item))
    sources.extend(_agent_bus_runtime_validation_sources(review_dispatch))
    sources.extend(_agent_task_sources(result, item=item, agent_task_store=agent_task_store))

    if not any(_contains_workflow_metadata(source) for source in sources):
        _log_workflow_chain_missing_source(
            source_object=sources,
            source_object_label="runtime normalization sources",
            reason="no_source_contains_workflow_metadata",
            runtime_validation_id=result.validation_id,
            workflow_id=result.workflow_id,
            work_item_id=result.work_item_id,
        )
        context["review_dispatch"] = review_dispatch
        return

    _fill_missing(review_dispatch, "workflow_chain_id", sources, "workflow_chain_id", "workflowChainId")
    _fill_missing(review_dispatch, "workflow_family", sources, "workflow_family", "workflowFamily")
    _fill_missing(review_dispatch, "workflow_step", sources, "workflow_step", "workflowStep", "current_workflow_step", "currentWorkflowStep")
    _fill_missing(review_dispatch, "current_workflow_step", sources, "current_workflow_step", "currentWorkflowStep", "workflow_step", "workflowStep")
    _fill_missing(review_dispatch, "workflow_steps", sources, "workflow_steps", "workflowSteps", "workflow_sequence", "workflowSequence")
    _fill_missing(review_dispatch, "workflow_sequence", sources, "workflow_sequence", "workflowSequence", "workflow_steps", "workflowSteps")
    _fill_missing(review_dispatch, "next_workflow_step", sources, "next_workflow_step", "nextWorkflowStep")
    _fill_missing(review_dispatch, "final_workflow_step", sources, "final_workflow_step", "finalWorkflowStep")
    _fill_missing(review_dispatch, "continuation_mode", sources, "continuation_mode", "continuationMode")
    _fill_missing(review_dispatch, "merge_gate", sources, "merge_gate", "mergeGate")
    _fill_missing(review_dispatch, "repository", sources, "repository", "repo", "repo_full_name", "repoFullName")
    _fill_missing(review_dispatch, "repo", sources, "repo", "repository", "repo_full_name", "repoFullName")
    _fill_missing(review_dispatch, "pr_number", sources, "pr_number", "prNumber")
    _fill_missing(review_dispatch, "branch", sources, "branch", "head_ref", "headRef")
    _fill_missing(review_dispatch, "base_branch", sources, "base_branch", "baseBranch")
    _fill_missing(review_dispatch, "previous_work_item_id", sources, "previous_work_item_id", "previousWorkItemId", "work_item_id", "workItemId", "agent_bus_work_item_id")
    _fill_missing(review_dispatch, "work_item_id", sources, "work_item_id", "workItemId", "agent_bus_work_item_id", "previous_work_item_id")

    review_dispatch.setdefault("repository", result.repo)
    review_dispatch.setdefault("repo", result.repo)
    if result.pr_number is not None:
        review_dispatch.setdefault("pr_number", result.pr_number)
    if result.branch:
        review_dispatch.setdefault("branch", result.branch)
    if result.base_branch:
        review_dispatch.setdefault("base_branch", result.base_branch)
    if result.work_item_id:
        review_dispatch.setdefault("work_item_id", result.work_item_id)
        review_dispatch.setdefault("previous_work_item_id", result.work_item_id)
    if result.workflow_id:
        review_dispatch.setdefault("workflow_id", result.workflow_id)

    workflow_chain = _workflow_chain_object(review_dispatch, sources)
    if workflow_chain:
        before_dispatch = review_dispatch.get("workflow_chain")
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_before",
            assignment="review_dispatch.workflow_chain",
            source_object=sources,
            source_object_label="runtime normalization sources",
            target_object=review_dispatch,
            before_value=before_dispatch,
            after_value=workflow_chain,
            runtime_validation_id=result.validation_id,
            workflow_id=result.workflow_id,
            work_item_id=result.work_item_id,
        )
        review_dispatch["workflow_chain"] = workflow_chain
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_after",
            assignment="review_dispatch.workflow_chain",
            source_object=sources,
            source_object_label="runtime normalization sources",
            target_object=review_dispatch,
            before_value=before_dispatch,
            after_value=review_dispatch.get("workflow_chain"),
            runtime_validation_id=result.validation_id,
            workflow_id=result.workflow_id,
            work_item_id=result.work_item_id,
        )
        before_context = context.get("workflow_chain")
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_before",
            assignment="runtime_context.workflow_chain",
            source_object=review_dispatch,
            source_object_label="review_dispatch",
            target_object=context,
            before_value=before_context,
            after_value=workflow_chain,
            runtime_validation_id=result.validation_id,
            workflow_id=result.workflow_id,
            work_item_id=result.work_item_id,
        )
        context["workflow_chain"] = workflow_chain
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_after",
            assignment="runtime_context.workflow_chain",
            source_object=review_dispatch,
            source_object_label="review_dispatch",
            target_object=context,
            before_value=before_context,
            after_value=context.get("workflow_chain"),
            runtime_validation_id=result.validation_id,
            workflow_id=result.workflow_id,
            work_item_id=result.work_item_id,
        )
    else:
        _log_workflow_chain_missing_source(
            source_object=sources,
            source_object_label="runtime normalization sources",
            reason="workflow_chain_object_empty",
            runtime_validation_id=result.validation_id,
            workflow_id=result.workflow_id,
            work_item_id=result.work_item_id,
        )

    metadata = dict(_dict_value(context.get("metadata")))
    if workflow_chain:
        before_metadata = metadata.get("workflow_chain")
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_before",
            assignment="metadata.workflow_chain",
            source_object=context,
            source_object_label="runtime_context",
            target_object=metadata,
            before_value=before_metadata,
            after_value=workflow_chain,
            runtime_validation_id=result.validation_id,
            workflow_id=result.workflow_id,
            work_item_id=result.work_item_id,
        )
        metadata.setdefault("workflow_chain", workflow_chain)
        _log_workflow_chain_assignment(
            event="workflow_chain_assignment_after",
            assignment="metadata.workflow_chain",
            source_object=context,
            source_object_label="runtime_context",
            target_object=metadata,
            before_value=before_metadata,
            after_value=metadata.get("workflow_chain"),
            runtime_validation_id=result.validation_id,
            workflow_id=result.workflow_id,
            work_item_id=result.work_item_id,
        )
    for key in _WORKFLOW_COMPANION_KEYS:
        value = review_dispatch.get(key)
        if _value_present(value):
            context.setdefault(key, value)
            metadata.setdefault(key, value)
    context["review_dispatch"] = review_dispatch
    if metadata:
        context["metadata"] = metadata
    log_event(
        "runtime_validation_workflow_metadata_normalized",
        runtime_validation_id=result.validation_id,
        workflow_id=context.get("workflow_id") or result.workflow_id,
        work_item_id=context.get("work_item_id") or result.work_item_id,
        repository=result.repo,
        pr_number=result.pr_number,
        branch=result.branch,
        workflow_chain_id=review_dispatch.get("workflow_chain_id"),
        workflow_step=review_dispatch.get("workflow_step"),
        next_workflow_step=review_dispatch.get("next_workflow_step"),
        workflow_chain_present=bool(workflow_chain),
        workflow_chain_length=len(workflow_chain),
    )


def _runtime_result_sources(result: RuntimeValidationResult) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    _append_nested_runtime_sources(sources, result.review_dispatch)
    try:
        bb2 = result.bb2.model_dump(mode="json")
    except Exception:
        bb2 = {}
    _append_nested_runtime_sources(sources, bb2)
    _append_nested_runtime_sources(sources, getattr(result.bb2, "review_context", None))
    return sources


def _append_nested_runtime_sources(sources: list[dict[str, Any]], value: Any) -> None:
    if not isinstance(value, dict) or value in sources:
        return
    sources.append(value)
    for key in (
        "metadata",
        "workflow_chain",
        "_workflow_chain",
        "workflowChain",
        "review_dispatch",
        "reviewDispatch",
        "runtime_context",
        "runtimeContext",
        "runtime_validation_context",
        "runtimeValidationContext",
        "bb2_packet",
        "review_context",
        "reviewContext",
        "agent_bus_work_item",
        "work_item",
    ):
        nested = value.get(key)
        if isinstance(nested, dict):
            _append_nested_runtime_sources(sources, nested)


def _agent_bus_work_item_sources(agent_bus_work_item: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(agent_bus_work_item, dict):
        return []
    sources: list[dict[str, Any]] = []
    _append_agent_bus_work_item_sources(sources, agent_bus_work_item)
    work_item = agent_bus_work_item.get("work_item")
    if isinstance(work_item, dict):
        _append_agent_bus_work_item_sources(sources, work_item)
    return sources


def _append_agent_bus_work_item_sources(sources: list[dict[str, Any]], work_item: dict[str, Any]) -> None:
    sources.append(work_item)
    metadata = work_item.get("metadata")
    if isinstance(metadata, dict):
        sources.append(metadata)
        workflow_chain = metadata.get("workflow_chain") or metadata.get("_workflow_chain") or metadata.get("workflowChain")
        if isinstance(workflow_chain, dict):
            sources.append(workflow_chain)
        runtime_context = metadata.get("runtime_context") or metadata.get("runtime_validation_context")
        if isinstance(runtime_context, dict):
            sources.append(runtime_context)
        review_dispatch = metadata.get("review_dispatch")
        if isinstance(review_dispatch, dict):
            sources.append(review_dispatch)
            nested_chain = review_dispatch.get("workflow_chain") or review_dispatch.get("_workflow_chain") or review_dispatch.get("workflowChain")
            if isinstance(nested_chain, dict):
                sources.append(nested_chain)


def _agent_bus_runtime_validation_sources(review_dispatch: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for key in ("agent_bus_runtime_validation", "agent_bus_work_item", "work_item"):
        value = review_dispatch.get(key)
        if isinstance(value, dict):
            sources.extend(_agent_bus_work_item_sources(value))
            history = value.get("history")
            if isinstance(history, list):
                for entry in history:
                    if isinstance(entry, dict):
                        sources.append(entry)
                        metadata = entry.get("metadata")
                        if isinstance(metadata, dict):
                            sources.append(metadata)
                            workflow_chain = metadata.get("workflow_chain")
                            if isinstance(workflow_chain, dict):
                                sources.append(workflow_chain)
    return sources


def _agent_task_sources(result: RuntimeValidationResult, *, item: ReviewWorkItem | None, agent_task_store: Any | None) -> list[dict[str, Any]]:
    if agent_task_store is None or not hasattr(agent_task_store, "list_agent_tasks"):
        return []
    try:
        tasks = agent_task_store.list_agent_tasks()
    except Exception:
        return []
    matched: list[dict[str, Any]] = []
    for task in tasks:
        if not _agent_task_matches(task, result, item=item):
            continue
        task_context = {
            "workflow_chain_id": getattr(task, "correlation_id", None),
            "workflow_id": getattr(task, "correlation_id", None),
            "repository": getattr(task, "repo_full_name", None),
            "repo": getattr(task, "repo_full_name", None),
            "issue_number": getattr(task, "issue_number", None),
            "branch": getattr(task, "branch", None),
            "commit_sha": getattr(task, "commit_sha", None),
            "work_item_id": getattr(task, "agent_bus_work_item_id", None),
            "previous_work_item_id": getattr(task, "agent_bus_work_item_id", None),
        }
        matched.append(task_context)
        evidence = getattr(task, "execution_evidence", None)
        if isinstance(evidence, dict):
            chain = evidence.get("_workflow_chain")
            if isinstance(chain, dict):
                matched.append(chain)
            nested_chain = evidence.get("workflow_chain")
            if isinstance(nested_chain, dict):
                matched.append(nested_chain)
            matched.append(evidence)
    return matched


def _agent_task_matches(task: Any, result: RuntimeValidationResult, *, item: ReviewWorkItem | None) -> bool:
    task_work_item_id = _string_or_none(getattr(task, "agent_bus_work_item_id", None))
    if result.work_item_id and task_work_item_id == result.work_item_id:
        return True
    task_id = _string_or_none(getattr(task, "task_id", None))
    candidate_ids = {result.work_item_id}
    if item is not None:
        candidate_ids.add(item.agent_bus_work_item_id)
    if task_id and task_id in candidate_ids:
        return True
    task_workflow_id = _string_or_none(getattr(task, "correlation_id", None))
    task_repo = _string_or_none(getattr(task, "repo_full_name", None))
    if result.workflow_id and task_workflow_id == result.workflow_id and (not task_repo or task_repo == result.repo):
        return True
    return False


def _contains_workflow_metadata(source: dict[str, Any]) -> bool:
    if not isinstance(source, dict):
        return False
    if any(_value_present(source.get(key)) for key in _WORKFLOW_CHAIN_KEYS):
        return True
    workflow_chain = source.get("workflow_chain") or source.get("workflowChain") or source.get("_workflow_chain")
    if isinstance(workflow_chain, dict) and any(_value_present(workflow_chain.get(key)) for key in _WORKFLOW_CHAIN_KEYS):
        return True
    metadata = source.get("metadata")
    if isinstance(metadata, dict):
        nested_chain = metadata.get("workflow_chain") or metadata.get("workflowChain") or metadata.get("_workflow_chain")
        if isinstance(nested_chain, dict) and any(_value_present(nested_chain.get(key)) for key in _WORKFLOW_CHAIN_KEYS):
            return True
        return any(_value_present(metadata.get(key)) for key in _WORKFLOW_CHAIN_KEYS)
    return False


def _fill_missing(target: dict[str, Any], key: str, sources: list[dict[str, Any]], *aliases: str) -> None:
    if _value_present(target.get(key)):
        return
    value = _first_present_from_sources(sources, *aliases)
    if _value_present(value):
        target[key] = value


def _workflow_chain_object(review_dispatch: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    raw_chain = _first_present_from_sources(sources, "workflow_chain", "workflowChain", "_workflow_chain")
    workflow_chain = dict(raw_chain) if isinstance(raw_chain, dict) else {}
    for key in _WORKFLOW_CHAIN_KEYS:
        value = review_dispatch.get(key)
        if _value_present(value):
            workflow_chain.setdefault(key, value)
    return workflow_chain


def _first_present_from_sources(sources: list[dict[str, Any]], *aliases: str) -> Any:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for alias in aliases:
            value = source.get(alias)
            if _value_present(value):
                return value
        metadata = source.get("metadata")
        if isinstance(metadata, dict):
            for alias in aliases:
                value = metadata.get(alias)
                if _value_present(value):
                    return value
            workflow_chain = metadata.get("workflow_chain") or metadata.get("workflowChain") or metadata.get("_workflow_chain")
            if isinstance(workflow_chain, dict):
                for alias in aliases:
                    value = workflow_chain.get(alias)
                    if _value_present(value):
                        return value
    return None


def _camelize(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _value_present(value: Any) -> bool:
    return value is not None and value != ""


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
