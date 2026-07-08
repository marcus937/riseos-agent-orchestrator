from collections.abc import Awaitable, Callable

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
        storage.save_review_work_item(response.work_item)
        log_event(
            "post_runtime_review_worker_after_persist_result",
            item_id=response.work_item.id,
            workflow_id=_workflow_id_from_item(response.work_item),
            runtime_validation_id=response.work_item.runtime_validation_id,
            status=str(response.work_item.status),
            lifecycle_stage=str(response.work_item.lifecycle_stage),
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
