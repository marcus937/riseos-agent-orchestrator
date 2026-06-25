from __future__ import annotations

from typing import Any

from app.circuit_runtime_validation import RuntimeValidationRequest
from app.config import Settings
from app.wf20_deployment_resume import (
    READY_DEPLOYMENT_STATES,
    WF20DeploymentWaitStore,
    _default_store,
    _deployment_state,
    _ignored_ready_deployment_result,
    _is_deployment_status_payload,
    _is_ready_deployment_resume,
    _log_deployment_status_received,
    _log_matched_waiting_workflow,
    _log_resuming_workflow,
    _log_starting_hermes,
    _log_workflow_already_resumed,
    _log_waiting,
    _original_trigger,
    _pending_result,
    _should_wait_for_deployment,
    _waiting_workflow_from_request,
)


def install_event_driven_wf20_runtime_validation(store: WF20DeploymentWaitStore | None = None) -> None:
    from app.wf20_runtime_validation import AgentBusRuntimeValidationStore
    import app.wf20_deployment_resume as base

    if store is not None:
        base._default_store = store
    original = base._original_trigger
    if original is None or getattr(AgentBusRuntimeValidationStore, "_wf20_event_driven_installed", False) is False:
        original = AgentBusRuntimeValidationStore.trigger
        base._original_trigger = original

    async def trigger(self: Any, request: RuntimeValidationRequest, settings: Settings) -> Any:
        active_store = getattr(self, "_deployment_wait_store", None) or base._default_store
        if _should_wait_for_deployment(request):
            workflow = _waiting_workflow_from_request(request)
            active_store.save_waiting(workflow)
            _log_waiting(workflow, request)
            return _pending_result(request, workflow)

        if _is_deployment_status_payload(request):
            _log_deployment_status_received(request)
            if not _is_ready_deployment_resume(request):
                state = _deployment_state(request).lower()
                reason = "Deployment status was not Ready."
                if state in READY_DEPLOYMENT_STATES and not request.target_url:
                    reason = "Ready deployment_status did not include a verified Preview URL."
                return _ignored_ready_deployment_result(request, reason)

            decision = active_store.claim_ready(request)
            if not decision.matched:
                return _ignored_ready_deployment_result(request, "No waiting WF20 workflow matched the Ready deployment.")
            if decision.already_resumed:
                _log_workflow_already_resumed(decision.workflow, request)
                return _ignored_ready_deployment_result(request, "Matching WF20 workflow was already resumed.")
            _log_matched_waiting_workflow(decision.workflow, request)
            _log_resuming_workflow(decision.workflow, request)
            _log_starting_hermes(decision.workflow, request)

        return await original(self, request, settings)

    AgentBusRuntimeValidationStore.trigger = trigger  # type: ignore[method-assign]
    AgentBusRuntimeValidationStore._wf20_event_driven_installed = True  # type: ignore[attr-defined]
