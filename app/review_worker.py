from collections.abc import Awaitable, Callable
from typing import Any

from app.config import Settings
from app.operational_logging import log_event, log_review_failed, log_worker_claimed
from app.review_queue import ReviewProcessResponse, ReviewWorkItem, review_queue
from app.storage import SQLiteStateStore
from app.workflow_chain_diagnostics import log_workflow_chain_availability

ReviewProcessor = Callable[[ReviewWorkItem, Settings], Awaitable[ReviewProcessResponse]]


async def process_queued_review_item(
    item_id: str,
    settings: Settings,
    storage: SQLiteStateStore | None,
    process_work_item: ReviewProcessor,
) -> ReviewProcessResponse | None:
    log_event(
        "post_runtime_review_worker_entered",
        item_id=item_id,
        storage_enabled=storage is not None,
        auto_review_processing_enabled=settings.enable_auto_review_processing,
        task_dispatch_enabled=settings.enable_task_dispatch,
        agent_bus_dispatch_enabled=settings.enable_agent_bus_dispatch,
    )
    item = _claim_review_work_item(item_id, storage)
    if item is None:
        log_event(
            "post_runtime_review_worker_exited",
            item_id=item_id,
            reason="claim_returned_none",
            storage_enabled=storage is not None,
        )
        return None

    log_event(
        "post_runtime_review_worker_claimed",
        item_id=item.id,
        workflow_id=_workflow_id_from_item(item),
        runtime_validation_id=item.runtime_validation_id,
        repository=item.repo_full_name,
        pr_number=item.pr_number,
        branch=item.branch,
        status=str(item.status),
        lifecycle_stage=str(item.lifecycle_stage),
    )
    _log_review_worker_workflow_chain_trace(
        "workflow_chain_trace_review_worker_after_claim",
        item,
        source_object="ReviewWorkItem after claim",
    )
    log_workflow_chain_availability(
        "wf_chain_metadata_review_worker_after_claim",
        item,
    )
    log_worker_claimed(item)
    try:
        log_event(
            "post_runtime_review_worker_before_process_work_item",
            item_id=item.id,
            workflow_id=_workflow_id_from_item(item),
            runtime_validation_id=item.runtime_validation_id,
            repository=item.repo_full_name,
            pr_number=item.pr_number,
            branch=item.branch,
            status=str(item.status),
        )
        _log_review_worker_workflow_chain_trace(
            "workflow_chain_trace_review_worker_before_process_work_item",
            item,
            source_object="ReviewWorkItem before process_work_item",
        )
        log_workflow_chain_availability(
            "wf_chain_metadata_review_worker_before_process_work_item",
            item,
        )
        response = await process_work_item(item, settings)
        log_event(
            "post_runtime_review_worker_after_process_work_item",
            item_id=response.work_item.id,
            workflow_id=_workflow_id_from_item(response.work_item),
            runtime_validation_id=response.work_item.runtime_validation_id,
            repository=response.work_item.repo_full_name,
            pr_number=response.work_item.pr_number,
            branch=response.work_item.branch,
            status=str(response.work_item.status),
            lifecycle_stage=str(response.work_item.lifecycle_stage),
            decision=response.decision.decision.value,
            github_writeback_attempted=response.github_writeback_attempted,
            github_writeback_success=response.github_writeback_success,
            task_dispatch_attempted=response.task_dispatch_attempted,
            task_dispatch_success=response.task_dispatch_success,
            agent_bus_dispatch_attempted=response.agent_bus_dispatch_attempted,
            agent_bus_dispatch_success=response.agent_bus_dispatch_success,
            continuation_id=getattr(response, "continuation_id", None),
        )
        _log_review_worker_workflow_chain_trace(
            "workflow_chain_trace_review_worker_after_process_work_item",
            response.work_item,
            source_object="ReviewWorkItem after process_work_item",
        )
        if response.work_item.runtime_validation_id and response.decision.decision.value == "approved_for_human_review" and not response.task_dispatch_attempted:
            log_event(
                "post_runtime_review_continuation_not_scheduled",
                item_id=response.work_item.id,
                workflow_id=_workflow_id_from_item(response.work_item),
                runtime_validation_id=response.work_item.runtime_validation_id,
                repository=response.work_item.repo_full_name,
                pr_number=response.work_item.pr_number,
                branch=response.work_item.branch,
                decision=response.decision.decision.value,
                github_writeback_attempted=response.github_writeback_attempted,
                github_writeback_success=response.github_writeback_success,
                github_writeback_error=response.github_writeback_error,
                task_dispatch_attempted=response.task_dispatch_attempted,
                task_dispatch_success=response.task_dispatch_success,
                task_dispatch_error=response.task_dispatch_error,
                agent_bus_dispatch_attempted=response.agent_bus_dispatch_attempted,
                agent_bus_dispatch_success=response.agent_bus_dispatch_success,
                agent_bus_dispatch_error=response.agent_bus_dispatch_error,
                reason="approved_runtime_review_returned_without_task_dispatch",
            )
        log_workflow_chain_availability(
            "wf_chain_metadata_review_worker_after_process_work_item",
            response.work_item,
        )
    except Exception as exc:
        log_event(
            "post_runtime_review_worker_exception",
            item_id=item.id,
            workflow_id=_workflow_id_from_item(item),
            runtime_validation_id=item.runtime_validation_id,
            repository=item.repo_full_name,
            pr_number=item.pr_number,
            branch=item.branch,
            exception_type=type(exc).__name__,
            error=str(exc),
        )
        retry_item = _reset_review_work_item_for_retry(item, storage, error=str(exc))
        log_review_failed(retry_item, error=str(exc))
        return None

    if storage is not None:
        log_event(
            "post_runtime_review_worker_before_persist_result",
            item_id=response.work_item.id,
            workflow_id=_workflow_id_from_item(response.work_item),
            runtime_validation_id=response.work_item.runtime_validation_id,
            status=str(response.work_item.status),
            lifecycle_stage=str(response.work_item.lifecycle_stage),
        )
        _log_review_worker_workflow_chain_trace(
            "workflow_chain_trace_post_runtime_worker_before_persist_result",
            response.work_item,
            source_object="ReviewWorkItem before post-runtime persist",
        )
        storage.save_review_work_item(response.work_item)
        log_event(
            "post_runtime_review_worker_after_persist_result",
            item_id=response.work_item.id,
            workflow_id=_workflow_id_from_item(response.work_item),
            runtime_validation_id=response.work_item.runtime_validation_id,
            status=str(response.work_item.status),
            lifecycle_stage=str(response.work_item.lifecycle_stage),
        )
        _log_review_worker_workflow_chain_trace(
            "workflow_chain_trace_post_runtime_worker_after_persist_result",
            response.work_item,
            source_object="ReviewWorkItem after post-runtime persist",
        )
    return response


def _claim_review_work_item(item_id: str, storage: SQLiteStateStore | None) -> ReviewWorkItem | None:
    if storage is not None:
        return storage.claim_review_work_item(item_id)
    return review_queue.claim_item(item_id)


def _reset_review_work_item_for_retry(
    item: ReviewWorkItem,
    storage: SQLiteStateStore | None,
    *,
    error: str | None = None,
) -> ReviewWorkItem:
    if storage is not None:
        return storage.reset_review_work_item_for_retry(item.id, error=error) or item
    return review_queue.reset_item_for_retry(item.id, error=error) or item


def _workflow_id_from_item(item: ReviewWorkItem) -> str | None:
    context = item.runtime_validation_context if isinstance(item.runtime_validation_context, dict) else {}
    value = context.get("workflow_id") or context.get("correlation_id")
    return str(value) if value else None


def _log_review_worker_workflow_chain_trace(event: str, item: ReviewWorkItem, *, source_object: str) -> None:
    summary = _workflow_chain_summary(item)
    log_event(
        event,
        source_object=source_object,
        item_id=item.id,
        workflow_id=_workflow_id_from_item(item),
        runtime_validation_id=item.runtime_validation_id,
        repository=item.repo_full_name,
        pr_number=item.pr_number,
        branch=item.branch,
        workflow_chain_exists=summary["exists"],
        workflow_chain_length=summary["length"],
        workflow_chain_first_step=summary["first_step"],
        workflow_chain_last_step=summary["last_step"],
        workflow_chain_keys=summary["keys"],
        workflow_chain_source_path=summary["source_path"],
        _include_nulls=True,
    )


def _workflow_chain_summary(source: Any) -> dict[str, Any]:
    chain, source_path = _find_workflow_chain(source)
    steps = _workflow_steps(chain)
    return {
        "exists": bool(chain),
        "length": len(steps) if steps else len(chain),
        "first_step": steps[0] if steps else _step_id(chain),
        "last_step": steps[-1] if steps else _step_id(chain),
        "keys": sorted(str(key) for key in chain.keys()) if isinstance(chain, dict) else [],
        "source_path": source_path,
    }


def _find_workflow_chain(source: Any) -> tuple[dict[str, Any], str | None]:
    candidates: list[tuple[Any, str]] = [(source, "source")]
    seen: set[int] = set()
    while candidates:
        value, path = candidates.pop(0)
        if id(value) in seen:
            continue
        seen.add(id(value))
        mapping = _as_dict(value)
        if not mapping:
            continue
        for key in ("workflow_chain", "_workflow_chain", "workflowChain"):
            nested = mapping.get(key)
            if isinstance(nested, dict) and nested:
                return nested, f"{path}.{key}"
        if _looks_like_workflow_chain(mapping):
            return mapping, path
        for key in (
            "runtime_validation_context",
            "runtimeValidationContext",
            "review_dispatch",
            "reviewDispatch",
            "runtime_context",
            "runtimeContext",
            "metadata",
            "bb2_packet",
            "review_context",
            "reviewContext",
        ):
            nested = mapping.get(key)
            if isinstance(nested, dict) and nested:
                candidates.append((nested, f"{path}.{key}"))
    return {}, None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(mode="json")
        except Exception:
            dumped = None
        if isinstance(dumped, dict):
            return dumped
    data = getattr(value, "__dict__", None)
    return data if isinstance(data, dict) else {}


def _looks_like_workflow_chain(value: dict[str, Any]) -> bool:
    return any(
        key in value
        for key in (
            "workflow_chain_id",
            "workflowChainId",
            "workflow_step",
            "workflowStep",
            "current_workflow_step",
            "currentWorkflowStep",
            "workflow_steps",
            "workflowSteps",
            "workflow_sequence",
            "workflowSequence",
        )
    )


def _workflow_steps(chain: dict[str, Any]) -> list[str]:
    raw_steps = chain.get("workflow_steps") or chain.get("workflowSteps") or chain.get("workflow_sequence") or chain.get("workflowSequence")
    if not isinstance(raw_steps, list):
        return []
    steps: list[str] = []
    for step in raw_steps:
        if isinstance(step, str):
            steps.append(step)
        elif isinstance(step, dict):
            step_id = step.get("id") or step.get("key") or step.get("workflow_step") or step.get("workflowStep") or step.get("name")
            if step_id is not None:
                steps.append(str(step_id))
    return steps


def _step_id(chain: dict[str, Any]) -> str | None:
    value = chain.get("workflow_step") or chain.get("workflowStep") or chain.get("current_workflow_step") or chain.get("currentWorkflowStep")
    return str(value) if value is not None else None
