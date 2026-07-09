from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from fastapi import FastAPI
from starlette.routing import Match

from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationRequest,
    RuntimeValidationResult,
    runtime_validation_store,
)
from app.clients.agent_bus import AgentBusClient
from app.config import Settings, get_settings
from app.operational_logging import log_event
from app.runtime_validation_agent_bus_bridge import advance_agent_bus_from_runtime_validation
from app.runtime_validation_handoff_trace import install_runtime_validation_handoff_trace
from app.runtime_validation_review_bridge import enqueue_review_from_runtime_validation
from app.review_worker import process_queued_review_item

install_runtime_validation_handoff_trace()

router = APIRouter(prefix="/api/v1/runtime-validations", tags=["runtime-validations"])
_RUNTIME_VALIDATION_ROUTE_PREFIX = "/api/v1/runtime-validations"
_RUNTIME_VALIDATION_ROUTE_PATHS = {
    "/api/v1/runtime-validations",
    "/api/v1/runtime-validations/{validation_id}",
    "/api/v1/runtime-validations/{validation_id}/evidence",
    "/api/v1/runtime-validations/{validation_id}/bb2-packet",
}


class _RoutePathMarker:
    def __init__(self, path: str) -> None:
        self.path = path

    def matches(self, scope: Any) -> tuple[Match, dict[str, Any]]:
        return Match.NONE, {}

    async def handle(self, scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("Route path marker is not request-handling middleware.")


def _require_runtime_admin_token(
    x_orchestrator_admin_token: Annotated[str | None, Header(alias="X-Orchestrator-Admin-Token")] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.orchestrator_admin_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ORCHESTRATOR_ADMIN_TOKEN is required before triggering runtime validations.",
        )
    if not x_orchestrator_admin_token or not hmac.compare_digest(
        x_orchestrator_admin_token,
        settings.orchestrator_admin_token,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid orchestrator admin token")


@router.post("", response_model=RuntimeValidationResult, status_code=status.HTTP_201_CREATED)
async def create_runtime_validation(
    request: RuntimeValidationRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
    _: None = Depends(_require_runtime_admin_token),
    settings: Settings = Depends(get_settings),
) -> RuntimeValidationResult:
    request = _request_with_default_base_branch(request, settings)
    store = _runtime_validation_store(http_request)
    _log_runtime_validation_store_selected(store, request, dispatch_path="runtime_validation_route")
    result = await store.trigger(request, settings)
    _ensure_created_response_has_handoff_outcome(result, store)
    storage = getattr(http_request.app.state, "storage", None)
    agent_task_store = getattr(http_request.app.state, "agent_task_store", None)
    agent_bus_work_item = await _agent_bus_work_item_for_runtime_result(result, http_request, settings)
    review_item = enqueue_review_from_runtime_validation(
        result,
        settings,
        storage=storage,
        agent_task_store=agent_task_store,
        agent_bus_work_item=agent_bus_work_item,
    )
    review_processing_scheduled = _schedule_runtime_review_processing(review_item, http_request, settings, storage, background_tasks)
    await advance_agent_bus_from_runtime_validation(result, settings)
    await _process_runtime_review_continuation_if_unscheduled(
        review_item,
        http_request,
        settings,
        storage,
        scheduled=review_processing_scheduled,
    )
    return result


@router.get("/{validation_id}", response_model=RuntimeValidationResult)
async def get_runtime_validation(
    validation_id: str,
    http_request: Request,
    _: None = Depends(_require_runtime_admin_token),
) -> RuntimeValidationResult:
    result = _runtime_validation_store(http_request).get(validation_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runtime validation not found")
    return result


@router.get("/{validation_id}/evidence", response_model=RuntimeValidationEvidenceSummary)
async def get_runtime_validation_evidence(
    validation_id: str,
    http_request: Request,
    _: None = Depends(_require_runtime_admin_token),
) -> RuntimeValidationEvidenceSummary:
    result = _runtime_validation_store(http_request).get(validation_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runtime validation not found")
    return result.evidence


@router.get("/{validation_id}/bb2-packet", response_model=RuntimeValidationBB2Packet)
async def get_runtime_validation_bb2_packet(
    validation_id: str,
    http_request: Request,
    _: None = Depends(_require_runtime_admin_token),
) -> RuntimeValidationBB2Packet:
    result = _runtime_validation_store(http_request).get(validation_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runtime validation not found")
    return result.bb2


def register_circuit_runtime_validation_routes(app: FastAPI) -> None:
    existing_paths = _registered_route_paths(app)
    if getattr(app.state, "circuit_runtime_validation_routes_registered", False) and _RUNTIME_VALIDATION_ROUTE_PATHS.issubset(existing_paths):
        return
    app.include_router(router)
    for route in app.router.routes:
        if not hasattr(route, "path"):
            setattr(route, "path", "")
    _add_route_path_markers(app)
    app.state.circuit_runtime_validation_routes_registered = True


def _runtime_validation_store(request: Request) -> Any:
    return getattr(request.app.state, "runtime_validation_store", runtime_validation_store)


async def _agent_bus_work_item_for_runtime_result(
    result: RuntimeValidationResult,
    request: Request,
    settings: Settings,
) -> dict[str, Any] | None:
    if not result.work_item_id or not settings.enable_agent_bus_dispatch:
        return None
    client = getattr(request.app.state, "agent_bus_client", None)
    owns_client = client is None
    if client is None:
        client = AgentBusClient(
            base_url=settings.agent_bus_base_url,
            token=settings.agent_bus_token,
            runtime_validation_token=settings.agent_bus_runtime_validation_token,
            timeout_seconds=settings.agent_bus_timeout_seconds,
        )
    try:
        work_item = await client.get_work_item(result.work_item_id)
        if isinstance(work_item, dict):
            log_event(
                "runtime_validation_agent_bus_work_item_metadata_loaded",
                runtime_validation_id=result.validation_id,
                workflow_id=result.workflow_id,
                work_item_id=result.work_item_id,
                repository=result.repo,
                pr_number=result.pr_number,
                branch=result.branch,
                metadata_keys=sorted(str(key) for key in (work_item.get("metadata") or {}).keys()) if isinstance(work_item.get("metadata"), dict) else [],
            )
            return work_item
    except Exception as exc:
        log_event(
            "runtime_validation_agent_bus_work_item_metadata_unavailable",
            runtime_validation_id=result.validation_id,
            workflow_id=result.workflow_id,
            work_item_id=result.work_item_id,
            repository=result.repo,
            pr_number=result.pr_number,
            branch=result.branch,
            error=str(exc),
            exception_type=exc.__class__.__name__,
        )
    finally:
        if owns_client and hasattr(client, "aclose"):
            await client.aclose()
    return None


def _schedule_runtime_review_processing(
    review_item: Any | None,
    request: Request,
    settings: Settings,
    storage: Any | None,
    background_tasks: BackgroundTasks,
) -> bool:
    if review_item is None:
        log_event("runtime_validation_review_processing_skipped", reason="no_review_item")
        return False
    if not settings.enable_auto_review_processing:
        log_event(
            "runtime_validation_review_processing_skipped",
            reason="auto_review_processing_disabled",
            work_item_id=getattr(review_item, "id", None),
            runtime_validation_id=getattr(review_item, "runtime_validation_id", None),
        )
        return False
    status_value = getattr(getattr(review_item, "status", None), "value", getattr(review_item, "status", None))
    if status_value != "pending_review":
        log_event(
            "runtime_validation_review_processing_skipped",
            reason="review_item_not_pending",
            work_item_id=getattr(review_item, "id", None),
            runtime_validation_id=getattr(review_item, "runtime_validation_id", None),
            review_item_status=status_value,
        )
        return False
    processor = _review_processor(request)
    if processor is None:
        log_event(
            "runtime_validation_review_processing_skipped",
            reason="review_processor_unavailable",
            work_item_id=getattr(review_item, "id", None),
            runtime_validation_id=getattr(review_item, "runtime_validation_id", None),
        )
        return False
    background_tasks.add_task(process_queued_review_item, review_item.id, settings, storage, processor)
    log_event(
        "runtime_validation_review_processing_scheduled",
        work_item_id=review_item.id,
        runtime_validation_id=getattr(review_item, "runtime_validation_id", None),
        repository=getattr(review_item, "repo_full_name", None),
        pr_number=getattr(review_item, "pr_number", None),
        branch=getattr(review_item, "branch", None),
    )
    return True


async def _process_runtime_review_continuation_if_unscheduled(
    review_item: Any | None,
    request: Request,
    settings: Settings,
    storage: Any | None,
    *,
    scheduled: bool,
) -> bool:
    if scheduled:
        return False
    if review_item is None:
        return False
    if not _review_item_has_workflow_chain(review_item):
        log_event(
            "runtime_validation_review_continuation_inline_skipped",
            reason="workflow_chain_metadata_missing",
            work_item_id=getattr(review_item, "id", None),
            runtime_validation_id=getattr(review_item, "runtime_validation_id", None),
        )
        return False
    status_value = getattr(getattr(review_item, "status", None), "value", getattr(review_item, "status", None))
    if status_value != "pending_review":
        log_event(
            "runtime_validation_review_continuation_inline_skipped",
            reason="review_item_not_pending",
            work_item_id=getattr(review_item, "id", None),
            runtime_validation_id=getattr(review_item, "runtime_validation_id", None),
            review_item_status=status_value,
        )
        return False
    processor = _review_processor(request)
    if processor is None:
        log_event(
            "runtime_validation_review_continuation_inline_skipped",
            reason="review_processor_unavailable",
            work_item_id=getattr(review_item, "id", None),
            runtime_validation_id=getattr(review_item, "runtime_validation_id", None),
        )
        return False
    log_event(
        "runtime_validation_review_continuation_inline_started",
        work_item_id=getattr(review_item, "id", None),
        runtime_validation_id=getattr(review_item, "runtime_validation_id", None),
        repository=getattr(review_item, "repo_full_name", None),
        pr_number=getattr(review_item, "pr_number", None),
        branch=getattr(review_item, "branch", None),
    )
    response = await process_queued_review_item(review_item.id, settings, storage, processor)
    log_event(
        "runtime_validation_review_continuation_inline_completed",
        work_item_id=getattr(review_item, "id", None),
        runtime_validation_id=getattr(review_item, "runtime_validation_id", None),
        processed=response is not None,
        task_dispatch_attempted=getattr(response, "task_dispatch_attempted", None) if response is not None else None,
        task_dispatch_success=getattr(response, "task_dispatch_success", None) if response is not None else None,
        agent_bus_dispatch_attempted=getattr(response, "agent_bus_dispatch_attempted", None) if response is not None else None,
        agent_bus_dispatch_success=getattr(response, "agent_bus_dispatch_success", None) if response is not None else None,
    )
    return response is not None


def _review_item_has_workflow_chain(review_item: Any) -> bool:
    context = getattr(review_item, "runtime_validation_context", None)
    if not isinstance(context, dict):
        return False
    review_dispatch = context.get("review_dispatch") if isinstance(context.get("review_dispatch"), dict) else {}
    metadata = context.get("metadata") if isinstance(context.get("metadata"), dict) else {}
    for value in (
        context.get("workflow_chain"),
        context.get("_workflow_chain"),
        context.get("workflowChain"),
        review_dispatch.get("workflow_chain"),
        review_dispatch.get("_workflow_chain"),
        review_dispatch.get("workflowChain"),
        metadata.get("workflow_chain"),
        metadata.get("_workflow_chain"),
        metadata.get("workflowChain"),
    ):
        if isinstance(value, dict) and value:
            return True
    return bool(
        context.get("workflow_chain_id")
        or context.get("workflow_step")
        or context.get("current_workflow_step")
        or review_dispatch.get("workflow_chain_id")
        or review_dispatch.get("workflow_step")
        or review_dispatch.get("current_workflow_step")
    )


def _review_processor(request: Request) -> Any | None:
    processor = getattr(request.app.state, "review_processor", None)
    if processor is not None:
        return processor
    try:
        from app import main as app_main
    except Exception:
        return None
    return getattr(app_main, "_process_work_item", None)


def _log_runtime_validation_store_selected(store: Any, request: RuntimeValidationRequest, *, dispatch_path: str) -> None:
    log_event(
        "runtime_validation_store_selected",
        dispatch_path=dispatch_path,
        store_class=store.__class__.__name__,
        store_module=store.__class__.__module__,
        repo=request.repo,
        pr_number=request.pr_number,
        branch=request.branch,
        base_branch=request.base_branch,
        work_item_id=request.work_item_id,
        evidence_id=request.evidence_id,
        workflow_id=request.workflow_id,
        validation_type=request.validation_type,
        target_url=request.target_url,
        commit_sha=getattr(request, "commit_sha", None),
    )


def _ensure_created_response_has_handoff_outcome(result: RuntimeValidationResult, store: Any) -> None:
    if _has_completed_hermes_outcome(result) or _has_explicit_skip_or_block_reason(result):
        log_event(
            "runtime_validation_created_response_contract_satisfied",
            runtime_validation_id=result.validation_id,
            workflow_id=result.workflow_id,
            work_item_id=result.work_item_id,
            evidence_packet_id=result.evidence_id,
            repository=result.repo,
            pr_number=result.pr_number,
            branch=result.branch,
            target_url=result.hermes.target_url,
            selected_store_class=store.__class__.__name__,
            selected_store_module=store.__class__.__module__,
            runtime_validation_status=result.status,
            hermes_status=result.hermes.status,
            hermes_job_id=result.hermes.job_id,
            skip_block_reason=result.error or result.hermes.error,
        )
        return
    log_event(
        "runtime_validation_created_response_contract_failed",
        runtime_validation_id=result.validation_id,
        workflow_id=result.workflow_id,
        work_item_id=result.work_item_id,
        evidence_packet_id=result.evidence_id,
        repository=result.repo,
        pr_number=result.pr_number,
        branch=result.branch,
        target_url=result.hermes.target_url,
        selected_store_class=store.__class__.__name__,
        selected_store_module=store.__class__.__module__,
        runtime_validation_status=result.status,
        hermes_status=result.hermes.status,
        hermes_job_id=result.hermes.job_id,
        skip_block_reason=result.error or result.hermes.error,
    )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "error": "runtime_validation_created_without_handoff_outcome",
            "runtime_validation_id": result.validation_id,
            "status": result.status,
            "hermes_status": result.hermes.status,
        },
    )


def _has_completed_hermes_outcome(result: RuntimeValidationResult) -> bool:
    return result.status == "completed" and result.hermes.status in {"PASSED", "FAILED"}


def _has_explicit_skip_or_block_reason(result: RuntimeValidationResult) -> bool:
    if result.status not in {"blocked", "failed", "pending"}:
        return False
    return bool(result.error or result.hermes.error)


def _request_with_default_base_branch(request: RuntimeValidationRequest, settings: Settings) -> RuntimeValidationRequest:
    if request.base_branch:
        return request
    branch = request.branch or settings.work_branch
    base_branch = settings.work_branch if request.pr_number is not None and branch != settings.work_branch else settings.base_branch
    return request.model_copy(update={"base_branch": base_branch})


def _registered_route_paths(app: FastAPI) -> set[str]:
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            paths.add(str(path))
        for child in getattr(route, "routes", []):
            child_path = getattr(child, "path", None)
            if child_path:
                paths.add(str(child_path))
    return paths


def _add_route_path_markers(app: FastAPI) -> None:
    existing_paths = _registered_route_paths(app)
    for path in sorted(_RUNTIME_VALIDATION_ROUTE_PATHS - existing_paths):
        app.router.routes.append(_RoutePathMarker(path))
