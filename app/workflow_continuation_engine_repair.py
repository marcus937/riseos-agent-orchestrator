from __future__ import annotations

from typing import Any

from app.agent_tasks import build_agent_task_store
from app.config import get_settings
from app.operational_logging import log_event
from app.reviewer.decision import ReviewDecisionType
from app import task_dispatch as task_dispatch_module

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


def install_workflow_continuation_engine_repair() -> None:
    if getattr(task_dispatch_module, "_wf20_continuation_engine_repair_installed", False):
        return
    task_dispatch_module.dispatch_workflow_chain_continuation = dispatch_workflow_chain_continuation  # type: ignore[assignment]
    task_dispatch_module._wf20_continuation_engine_repair_installed = True  # type: ignore[attr-defined]


async def dispatch_workflow_chain_continuation(
    item: Any,
    decision: ReviewDecisionType,
    *,
    enabled: bool,
    agent_bus_client: task_dispatch_module.AgentBusDispatchClient | None,
    agent_bus_enabled: bool,
    continuation_store: task_dispatch_module.WorkflowContinuationStore | None = None,
    base_branch: str = task_dispatch_module.WF_CHAIN_BASE_BRANCH,
) -> task_dispatch_module.TaskDispatchResult | None:
    _hydrate_workflow_chain_context_from_agent_tasks(item, _agent_task_store_for_continuation())
    missing_reason = task_dispatch_module.workflow_chain_missing_metadata_reason(item)
    _log_continuation_preflight(item, missing_reason=missing_reason)
    if missing_reason is not None:
        return task_dispatch_module._record_missing_metadata_continuation(item, continuation_store, base_branch=base_branch, reason=missing_reason)

    continuation_context = task_dispatch_module.workflow_chain_continuation_for_decision(item, decision, base_branch=base_branch)
    if continuation_context is None:
        if task_dispatch_module.workflow_chain_context_from_item(item, base_branch=base_branch) is not None:
            log_event("CONTINUATION_SKIPPED_FINAL_STEP", **task_dispatch_module._workflow_chain_log_context(item))
        return None
    if not enabled:
        return task_dispatch_module.TaskDispatchResult()
    if continuation_store is None:
        return task_dispatch_module.TaskDispatchResult(
            attempted=True,
            success=False,
            error="Workflow continuation store is required for workflow chain continuation.",
        )

    continuation, created = continuation_store.resolve_or_create_workflow_continuation(
        task_dispatch_module.build_workflow_continuation_payload(continuation_context)
    )
    existing_work_item_id = continuation.next_work_item_id or continuation.current_work_item_id
    if decision == ReviewDecisionType.NEEDS_CHANGES and existing_work_item_id:
        continuation = continuation_store.mark_workflow_continuation_changes_requested(continuation.continuation_id)
        return task_dispatch_module._continuation_result(continuation, success=True, agent_bus_attempted=False)
    if not created and continuation.status in task_dispatch_module._EXISTING_WORK_STATUSES and existing_work_item_id:
        return task_dispatch_module._continuation_result(continuation, success=True, agent_bus_attempted=False)

    return await task_dispatch_module._dispatch_existing_workflow_continuation(
        continuation,
        agent_bus_client=agent_bus_client,
        agent_bus_enabled=agent_bus_enabled,
        continuation_store=continuation_store,
    )


def _agent_task_store_for_continuation() -> Any | None:
    try:
        settings = get_settings()
        return build_agent_task_store(settings.orchestrator_db_path)
    except Exception:
        return None


def _hydrate_workflow_chain_context_from_agent_tasks(item: Any, agent_task_store: Any | None) -> None:
    if task_dispatch_module.workflow_chain_context_from_item(item) is not None:
        return
    task = _matching_agent_task(item, agent_task_store)
    if task is None:
        return
    evidence = getattr(task, "execution_evidence", None)
    if not isinstance(evidence, dict):
        return
    workflow_chain = evidence.get("_workflow_chain") if isinstance(evidence.get("_workflow_chain"), dict) else evidence.get("workflow_chain")
    if not isinstance(workflow_chain, dict) or not workflow_chain:
        return

    runtime_context = dict(getattr(item, "runtime_validation_context", None) or {})
    review_dispatch = dict(runtime_context.get("review_dispatch") or {})
    hydrated_chain = dict(workflow_chain)
    _fill_workflow_context_defaults(hydrated_chain, item, task, runtime_context, review_dispatch)

    for key, value in hydrated_chain.items():
        if value is None or value == "":
            continue
        if key in _WORKFLOW_CHAIN_KEYS or key in {"repository", "repo", "pr_number", "branch", "base_branch", "work_item_id", "previous_work_item_id"}:
            runtime_context.setdefault(key, value)
            review_dispatch.setdefault(key, value)
    runtime_context["workflow_chain"] = hydrated_chain
    review_dispatch["workflow_chain"] = hydrated_chain
    runtime_context["review_dispatch"] = review_dispatch
    item.runtime_validation_context = runtime_context
    log_event(
        "CONTINUATION_METADATA_RECOVERED",
        workflow_id=hydrated_chain.get("workflow_chain_id") or runtime_context.get("workflow_id"),
        work_item_id=runtime_context.get("work_item_id") or getattr(item, "agent_bus_work_item_id", None),
        current_workflow_step=hydrated_chain.get("current_workflow_step") or hydrated_chain.get("workflow_step"),
        next_workflow_step=hydrated_chain.get("next_workflow_step"),
        workflow_chain_length=len(hydrated_chain.get("workflow_steps") or hydrated_chain.get("workflow_sequence") or hydrated_chain),
        workflow_chain_keys=sorted(str(key) for key in hydrated_chain.keys()),
    )


def _matching_agent_task(item: Any, agent_task_store: Any | None) -> Any | None:
    if agent_task_store is None or not hasattr(agent_task_store, "list_agent_tasks"):
        return None
    try:
        tasks = agent_task_store.list_agent_tasks()
    except Exception:
        return None
    runtime_context = dict(getattr(item, "runtime_validation_context", None) or {})
    candidate_work_item_ids = {
        _string_or_none(getattr(item, "agent_bus_work_item_id", None)),
        _string_or_none(runtime_context.get("work_item_id")),
        _string_or_none(runtime_context.get("agent_bus_work_item_id")),
    }
    candidate_work_item_ids.discard(None)
    for task in tasks:
        if _string_or_none(getattr(task, "agent_bus_work_item_id", None)) in candidate_work_item_ids:
            return task

    workflow_ids = {
        _string_or_none(runtime_context.get("workflow_id")),
        _string_or_none(runtime_context.get("correlation_id")),
    }
    workflow_ids.discard(None)
    repo = _string_or_none(getattr(item, "repo_full_name", None)) or _string_or_none(runtime_context.get("repository")) or _string_or_none(runtime_context.get("repo"))
    for task in tasks:
        if _string_or_none(getattr(task, "correlation_id", None)) not in workflow_ids:
            continue
        task_repo = _string_or_none(getattr(task, "repo_full_name", None))
        if repo and task_repo and repo != task_repo:
            continue
        return task
    return None


def _fill_workflow_context_defaults(
    workflow_chain: dict[str, Any],
    item: Any,
    task: Any,
    runtime_context: dict[str, Any],
    review_dispatch: dict[str, Any],
) -> None:
    workflow_chain.setdefault("workflow_chain_id", getattr(task, "correlation_id", None) or runtime_context.get("workflow_id"))
    workflow_chain.setdefault("workflow_step", workflow_chain.get("current_workflow_step"))
    workflow_chain.setdefault("current_workflow_step", workflow_chain.get("workflow_step"))
    workflow_chain.setdefault("repository", getattr(item, "repo_full_name", None) or getattr(task, "repo_full_name", None) or runtime_context.get("repository") or runtime_context.get("repo"))
    workflow_chain.setdefault("repo", workflow_chain.get("repository"))
    workflow_chain.setdefault("pr_number", getattr(item, "pr_number", None) or runtime_context.get("pr_number") or review_dispatch.get("pr_number"))
    workflow_chain.setdefault("branch", getattr(item, "branch", None) or runtime_context.get("branch") or getattr(task, "branch", None))
    workflow_chain.setdefault("base_branch", getattr(item, "base_branch", None) or runtime_context.get("base_branch"))
    workflow_chain.setdefault("work_item_id", getattr(item, "agent_bus_work_item_id", None) or runtime_context.get("work_item_id"))
    workflow_chain.setdefault("previous_work_item_id", workflow_chain.get("work_item_id"))


def _log_continuation_preflight(item: Any, *, missing_reason: str | None) -> None:
    context = task_dispatch_module.workflow_chain_context_from_item(item) or {}
    runtime_context = dict(getattr(item, "runtime_validation_context", None) or {})
    review_dispatch = dict(runtime_context.get("review_dispatch") or {})
    workflow_chain = task_dispatch_module._canonical_workflow_chain_from_context(review_dispatch, runtime_context)
    workflow_steps = context.get("workflow_steps") or task_dispatch_module._workflow_steps_from_context(workflow_chain, review_dispatch, runtime_context) or []
    current_step = context.get("workflow_step") or "UNKNOWN"
    next_step = context.get("next_workflow_step")
    if current_step != "UNKNOWN" and not next_step:
        next_step = task_dispatch_module.next_workflow_chain_step(current_step, workflow_steps=workflow_steps)
    fields = {
        "workflow_id": context.get("workflow_chain_id") or workflow_chain.get("workflow_chain_id") or runtime_context.get("workflow_id"),
        "work_item_id": context.get("previous_work_item_id") or runtime_context.get("work_item_id") or getattr(item, "agent_bus_work_item_id", None),
        "current_workflow_step": current_step,
        "next_workflow_step": next_step or "UNKNOWN",
        "continuation_id": None,
        "workflow_chain_length": len(workflow_steps or workflow_chain),
        "workflow_chain_keys": sorted(str(key) for key in workflow_chain.keys()),
    }
    if current_step == "UNKNOWN":
        fields["unknown_reason"] = missing_reason or "WORKFLOW_STEP_NOT_RESOLVED"
    log_event("CONTINUATION_PREFLIGHT", **fields, _include_nulls=True)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
