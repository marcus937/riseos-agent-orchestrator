from collections.abc import Awaitable, Callable

from app.config import Settings
from app.operational_logging import log_review_failed, log_worker_claimed
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
    item = _claim_review_work_item(item_id, storage)
    if item is None:
        return None

    log_workflow_chain_availability(
        "wf_chain_metadata_review_worker_after_claim",
        item,
    )
    log_worker_claimed(item)
    try:
        log_workflow_chain_availability(
            "wf_chain_metadata_review_worker_before_process_work_item",
            item,
        )
        response = await process_work_item(item, settings)
        log_workflow_chain_availability(
            "wf_chain_metadata_review_worker_after_process_work_item",
            response.work_item,
        )
    except Exception as exc:
        retry_item = _reset_review_work_item_for_retry(item, storage, error=str(exc))
        log_review_failed(retry_item, error=str(exc))
        return None

    if storage is not None:
        storage.save_review_work_item(response.work_item)
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
