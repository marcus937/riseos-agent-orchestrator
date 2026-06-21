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
from app.runtime_validation_review_bridge import enqueue_review_from_runtime_validation

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


@router.post("", response_model=RuntimeValidationResult)
async def create_runtime_validation(
    request: RuntimeValidationRequest,
    http_request: Request,
    _: None = Depends(_require_runtime_admin_token),
    settings: Settings = Depends(get_settings),
) -> RuntimeValidationResult:
    request = _request_with_default_base_branch(request, settings)
    result = await runtime_validation_store.trigger(request, settings)
    enqueue_review_from_runtime_validation(
        result,
        settings,
        storage=getattr(http_request.app.state, "storage", None),
    )
    return result


@router.get("/{validation_id}", response_model=RuntimeValidationResult)
async def get_runtime_validation(
    validation_id: str,
    _: None = Depends(_require_runtime_admin_token),
) -> RuntimeValidationResult:
    result = runtime_validation_store.get(validation_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runtime validation not found")
    return result


@router.get("/{validation_id}/evidence", response_model=RuntimeValidationEvidenceSummary)
async def get_runtime_validation_evidence(
    validation_id: str,
    _: None = Depends(_require_runtime_admin_token),
) -> RuntimeValidationEvidenceSummary:
    result = runtime_validation_store.get(validation_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runtime validation not found")
    return result.evidence


@router.get("/{validation_id}/bb2-packet", response_model=RuntimeValidationBB2Packet)
async def get_runtime_validation_bb2_packet(
    validation_id: str,
    _: None = Depends(_require_runtime_admin_token),
) -> RuntimeValidationBB2Packet:
    result = runtime_validation_store.get(validation_id)
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
