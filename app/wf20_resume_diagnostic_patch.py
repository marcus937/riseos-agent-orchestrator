from __future__ import annotations

from typing import Any

from app.circuit_runtime_validation import RuntimeValidationRequest
from app.config import Settings
from app.wf20_resume_diagnostics import (
    TERMINAL_NO_READY_DEPLOYMENT,
    TERMINAL_NO_VERIFIED_PREVIEW,
    log_hermes_not_launched,
    log_starting_hermes,
    log_waiting_for_deployment,
)

_BLOCKING_TARGET_SOURCES = {"vercel_failed", "vercel_timeout", "vercel_preview_pending"}


def install_wf20_resume_diagnostic_patch() -> None:
    from app.wf20_runtime_validation import AgentBusRuntimeValidationStore

    if getattr(AgentBusRuntimeValidationStore, "_wf20_resume_diagnostic_patch_installed", False):
        return
    original_trigger = AgentBusRuntimeValidationStore.trigger

    async def trigger(self: Any, request: RuntimeValidationRequest, settings: Settings) -> Any:
        target_source = request.target_url_source or "missing"
        if request.target_url is None and target_source in _BLOCKING_TARGET_SOURCES:
            reason = request.target_url_pending_reason or "No Ready deployment with a verified Preview URL was available."
            log_waiting_for_deployment(
                request,
                reason=reason,
                runtime_validation_id=_runtime_validation_id(request),
                pending_store_size_after_insert=_pending_store_size(self),
            )
            terminal_reason = TERMINAL_NO_READY_DEPLOYMENT if target_source == "vercel_failed" else TERMINAL_NO_VERIFIED_PREVIEW
            log_hermes_not_launched(
                terminal_reason,
                request,
                reason=reason,
                target_url_source=target_source,
                runtime_validation_id=_runtime_validation_id(request),
            )
        elif request.target_url and not getattr(AgentBusRuntimeValidationStore, "_wf20_event_driven_installed", False):
            log_starting_hermes(request, runtime_validation_id=_runtime_validation_id(request))

        return await original_trigger(self, request, settings)

    AgentBusRuntimeValidationStore.trigger = trigger  # type: ignore[method-assign]
    AgentBusRuntimeValidationStore._wf20_resume_diagnostic_patch_installed = True  # type: ignore[attr-defined]


def _runtime_validation_id(request: RuntimeValidationRequest) -> str:
    workflow_id = request.workflow_id or request.correlation_id or "unknown"
    return f"wf20-diagnostic-{workflow_id}"


def _pending_store_size(store: Any) -> int:
    items = getattr(store, "_items", None)
    if isinstance(items, dict):
        return len(items)
    return 0
