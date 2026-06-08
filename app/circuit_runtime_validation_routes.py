from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi import FastAPI

from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationRequest,
    RuntimeValidationResult,
    runtime_validation_store,
)
from app.config import Settings, get_settings
from app.workflow_routes import register_workflow_routes

router = APIRouter(prefix="/api/v1/runtime-validations", tags=["runtime-validations"])


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
    _: None = Depends(_require_runtime_admin_token),
    settings: Settings = Depends(get_settings),
) -> RuntimeValidationResult:
    return await runtime_validation_store.trigger(request, settings)


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
    if getattr(app.state, "circuit_runtime_validation_routes_registered", False):
        return
    app.include_router(router)
    app.state.circuit_runtime_validation_routes_registered = True
    register_workflow_routes(app)
