from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationHermesSummary,
    RuntimeValidationRequest,
    RuntimeValidationResult,
)
from app.config import Settings
from app.wf20_deployment_resume import is_waiting_for_deployment_request


def install_event_driven_wf20_runtime_validation() -> None:
    """Install the WF20 deployment-wait guard on the active runtime store.

    The ReviewQueue webhook path owns durable correlation and resume. This hook
    preserves the event-driven WF20 runtime behavior for direct runtime-validation
    dispatches by preventing pending deployment requests from being treated as
    terminal Hermes failures before a deployment_status webhook can resume them.
    """

    from app.wf20_runtime_validation import AgentBusRuntimeValidationStore

    if getattr(AgentBusRuntimeValidationStore, "_wf20_event_driven_installed", False):
        return
    original_trigger = AgentBusRuntimeValidationStore.trigger

    async def trigger(self: Any, request: RuntimeValidationRequest, settings: Settings) -> RuntimeValidationResult:
        if is_waiting_for_deployment_request(request):
            return _pending_waiting_result(request)
        return await original_trigger(self, request, settings)

    AgentBusRuntimeValidationStore.trigger = trigger  # type: ignore[method-assign]
    AgentBusRuntimeValidationStore._wf20_event_driven_installed = True  # type: ignore[attr-defined]


def _pending_waiting_result(request: RuntimeValidationRequest) -> RuntimeValidationResult:
    now = datetime.now(UTC)
    workflow_id = request.workflow_id or request.correlation_id or f"wf20-{request.repo}-{request.pr_number or request.branch or 'unknown'}"
    correlation_id = request.correlation_id or workflow_id
    reason = request.target_url_pending_reason or "Waiting for Ready Vercel Preview deployment_status webhook."
    return RuntimeValidationResult(
        validation_id=f"wf20-waiting-{workflow_id}",
        status="pending",
        repo=request.repo,
        issue_number=request.issue_number,
        pr_number=request.pr_number,
        branch=request.branch,
        base_branch=request.base_branch,
        work_item_id=request.work_item_id,
        evidence_id=request.evidence_id,
        review_agent=request.review_agent,
        workflow_id=workflow_id,
        review_dispatch=request.review_dispatch,
        validation_type=request.validation_type,
        requested_by=request.requested_by,
        created_at=now,
        correlation_id=correlation_id,
        hermes=RuntimeValidationHermesSummary(
            target_url=None,
            target_source=request.target_url_source,
            status="SKIPPED",
            error=reason,
        ),
        evidence=RuntimeValidationEvidenceSummary(error=reason),
        bb2=RuntimeValidationBB2Packet(review_status="pending"),
        error=reason,
    )
