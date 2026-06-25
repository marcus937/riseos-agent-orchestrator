from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi import FastAPI
from starlette.routing import Match

from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationRequest,
    RuntimeValidationResult,
    runtime_validation_store,
)
from app.config import Settings, get_settings
from app.operational_logging import log_event
from app.runtime_validation_handoff_trace import install_runtime_validation_handoff_trace
from app.runtime_validation_review_bridge import enqueue_review_from_runtime_validation

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
    _: None = Depends(_require_runtime_admin_token),
    settings: Settings = Depends(get_settings),
) -> RuntimeValidationResult:
    request = _request_with_default_base_branch(request, settings)
    store = _runtime_validation_store(http_request)
    _log_runtime_validation_store_selected(store, request, dispatch_path="runtime_validation_route")
    result = await store.trigger(request, settings)
    _ensure_created_response_has_handoff_outcome(result, store)
    enqueue_review_from_runtime_validation(
        result,
        settings,
        storage=getattr(http_request.app.state, "storage", None),
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
