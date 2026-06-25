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
from app.operational_logging import log_event
from app.wf20_deployment_resume import is_waiting_for_deployment_request


def install_event_driven_wf20_runtime_validation() -> None:
    """Install the canonical WF20/WF21 deployment-resume guard.

    The ReviewQueue webhook path owns durable WAITING_FOR_DEPLOYMENT
    persistence and deployment_status correlation. This hook keeps the active
    runtime store aligned with that state machine: pending deployment requests
    remain non-terminal, Ready deployment requests emit the Hermes-start
    lifecycle, and duplicate Ready events in the same process return the
    existing runtime validation result instead of launching Hermes again.
    """

    from app.wf20_runtime_validation import AgentBusRuntimeValidationStore

    if getattr(AgentBusRuntimeValidationStore, "_wf20_event_driven_installed", False):
        return
    original_trigger = AgentBusRuntimeValidationStore.trigger

    async def trigger(self: Any, request: RuntimeValidationRequest, settings: Settings) -> RuntimeValidationResult:
        if is_waiting_for_deployment_request(request):
            result = _pending_waiting_result(request)
            _store_result(self, result)
            return result
        if request.target_url:
            existing = _find_resumed_runtime_validation(getattr(self, "_items", {}).values(), request)
            if existing is not None:
                _log_workflow_already_resumed(request, existing)
                return existing
            _log_deployment_ready(request)
            _log_starting_hermes(request)
        result = await original_trigger(self, request, settings)
        if request.target_url:
            _store_result(self, result)
        return result

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


def _find_resumed_runtime_validation(results: Any, request: RuntimeValidationRequest) -> RuntimeValidationResult | None:
    for result in results:
        if not isinstance(result, RuntimeValidationResult):
            continue
        if result.repo != request.repo:
            continue
        if result.workflow_id and request.workflow_id and result.workflow_id != request.workflow_id:
            continue
        if result.pr_number is not None and request.pr_number is not None and result.pr_number != request.pr_number:
            continue
        if result.branch and request.branch and result.branch != request.branch:
            continue
        if result.hermes.target_url == request.target_url and result.status in {"pending", "completed", "failed", "blocked"}:
            return result
    return None


def _store_result(store: Any, result: RuntimeValidationResult) -> None:
    items = getattr(store, "_items", None)
    if isinstance(items, dict):
        items[result.validation_id] = result


def _log_deployment_ready(request: RuntimeValidationRequest) -> None:
    log_event(
        "DEPLOYMENT_READY",
        workflow_id=request.workflow_id,
        repo=request.repo,
        pr=request.pr_number,
        pr_number=request.pr_number,
        branch=request.branch,
        commit_sha=_commit_sha(request),
        preview_url=request.target_url,
        target_url=request.target_url,
        target_url_source=request.target_url_source,
    )


def _log_starting_hermes(request: RuntimeValidationRequest) -> None:
    log_event(
        "STARTING_HERMES",
        workflow_id=request.workflow_id,
        repo=request.repo,
        pr=request.pr_number,
        pr_number=request.pr_number,
        branch=request.branch,
        commit_sha=_commit_sha(request),
        preview_url=request.target_url,
    )


def _log_workflow_already_resumed(request: RuntimeValidationRequest, result: RuntimeValidationResult) -> None:
    log_event(
        "WORKFLOW_ALREADY_RESUMED",
        workflow_id=request.workflow_id,
        repo=request.repo,
        pr=request.pr_number,
        pr_number=request.pr_number,
        branch=request.branch,
        commit_sha=_commit_sha(request),
        preview_url=request.target_url,
        validation_id=result.validation_id,
    )


def _commit_sha(request: RuntimeValidationRequest) -> str | None:
    value = getattr(request, "commit_sha", None)
    return str(value) if value else None
