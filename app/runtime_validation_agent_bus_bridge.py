"""Bridge Hermes runtime validation results into the Agent Bus lifecycle.

Hermes PASSED is treated as approval for the Agent Bus workflow gate only: it
means the runtime validation evidence is sufficient to advance and complete the
original WorkItem. It is intentionally not PR merge authorization and must not be
used to merge GitHub pull requests or bypass human/BB2 code-review policy.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.circuit_runtime_validation import RuntimeValidationResult
from app.clients.agent_bus import AgentBusClient
from app.config import Settings
from app.operational_logging import log_event


class AgentBusLifecycleClient(Protocol):
    async def get_work_item(self, work_item_id: str) -> dict[str, Any]: ...

    async def create_review_packet(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def attach_review_to_work_item(self, work_item_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def transition_work_item(
        self,
        work_item_id: str,
        *,
        status: str,
        actor: str,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        owner_agent: str | None = None,
        review_agent: str | None = None,
    ) -> dict[str, Any]: ...

    async def complete_work_item(
        self,
        work_item_id: str,
        *,
        actor: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


_TERMINAL_RUNTIME_STATUSES = {"completed", "failed", "blocked"}
_PASSING_HERMES_STATUS = "PASSED"
_ORCHESTRATOR_ACTOR = "riseos-agent-orchestrator"


async def advance_agent_bus_from_runtime_validation(
    result: RuntimeValidationResult,
    settings: Settings,
    *,
    agent_bus_client: AgentBusLifecycleClient | None = None,
) -> dict[str, Any] | None:
    """Advance the original Agent Bus WorkItem after terminal Hermes validation.

    A passing Hermes result is intentionally equivalent to BB2 approval only for
    Agent Bus workflow progression. It authorizes READY_FOR_REVIEW through
    COMPLETED transitions for the WorkItem; it does not authorize merging a PR.
    """

    context = _bridge_context(result, settings)
    if not settings.enable_agent_bus_dispatch:
        _log_skipped(context, reason="agent_bus_dispatch_disabled")
        return None
    if not result.work_item_id:
        _log_skipped(context, reason="missing_work_item_id")
        return None
    if result.status not in _TERMINAL_RUNTIME_STATUSES:
        _log_skipped(context, reason="runtime_validation_not_terminal")
        return None

    owns_client = agent_bus_client is None
    client = agent_bus_client or AgentBusClient(
        base_url=settings.agent_bus_base_url,
        token=settings.agent_bus_token,
        runtime_validation_token=settings.agent_bus_runtime_validation_token,
        timeout_seconds=settings.agent_bus_timeout_seconds,
    )
    try:
        work_item = await client.get_work_item(result.work_item_id)
        status = _work_item_status(work_item)
        log_event("agent_bus_runtime_bridge_started", **context, agent_bus_status=status)

        if status == "COMPLETED":
            _log_skipped(context, reason="work_item_already_completed", agent_bus_status=status)
            return {"status": status, "skipped": True, "reason": "work_item_already_completed"}

        if _hermes_passed(result):
            return await _advance_passed_validation(client, result, settings, context, status)

        await _attach_non_passing_review(client, result, settings, context)
        _log_skipped(
            context,
            reason="runtime_validation_not_passed",
            hermes_status=result.hermes.status,
            runtime_validation_status=result.status,
        )
        return {"status": status, "skipped": True, "reason": "runtime_validation_not_passed"}
    except Exception as exc:
        log_event(
            "agent_bus_runtime_bridge_failed",
            **context,
            error=str(exc),
            exception_type=exc.__class__.__name__,
        )
        return None
    finally:
        if owns_client and hasattr(client, "aclose"):
            await client.aclose()  # type: ignore[attr-defined]


async def _advance_passed_validation(
    client: AgentBusLifecycleClient,
    result: RuntimeValidationResult,
    settings: Settings,
    context: dict[str, Any],
    status: str | None,
) -> dict[str, Any]:
    reviewer = _reviewer(result, settings)
    packet_response = await client.create_review_packet(_review_packet_payload(result, settings, review_status="approved"))
    review_packet_id = _response_identifier(packet_response)
    log_event(
        "agent_bus_runtime_bridge_review_packet_created",
        **context,
        review_packet_id=review_packet_id,
        review_status="approved",
    )
    await client.attach_review_to_work_item(
        result.work_item_id or "",
        _review_attachment_payload(result, review_packet_id, packet_response, review_status="approved"),
    )

    status = await _transition_until_reviewed(client, result, settings, context, status, reviewer)
    if status == "APPROVED":
        await client.complete_work_item(result.work_item_id or "", actor=_ORCHESTRATOR_ACTOR, metadata=_bridge_metadata(result))
        log_event("agent_bus_runtime_bridge_completed", **context, from_status="APPROVED", to_status="COMPLETED")
        return {"status": "COMPLETED", "completed": True, "review_packet_id": review_packet_id}

    if status == "COMPLETED":
        _log_skipped(context, reason="work_item_already_completed", agent_bus_status=status)
        return {"status": status, "skipped": True, "reason": "work_item_already_completed"}

    _log_skipped(context, reason="unsupported_work_item_status", agent_bus_status=status)
    return {"status": status, "skipped": True, "reason": "unsupported_work_item_status"}


async def _transition_until_reviewed(
    client: AgentBusLifecycleClient,
    result: RuntimeValidationResult,
    settings: Settings,
    context: dict[str, Any],
    status: str | None,
    reviewer: str,
) -> str | None:
    sequence = _transition_sequence(status)
    current = status
    for target in sequence:
        await client.transition_work_item(
            result.work_item_id or "",
            status=target,
            actor=_ORCHESTRATOR_ACTOR,
            reason="Hermes runtime validation passed.",
            metadata=_bridge_metadata(result),
            review_agent=reviewer if target in {"REVIEW_IN_PROGRESS", "APPROVED"} else None,
        )
        log_event(
            "agent_bus_runtime_bridge_transitioned",
            **context,
            from_status=current,
            to_status=target,
        )
        current = target
    return current


async def _attach_non_passing_review(
    client: AgentBusLifecycleClient,
    result: RuntimeValidationResult,
    settings: Settings,
    context: dict[str, Any],
) -> None:
    review_status = "blocked" if result.status == "blocked" or result.hermes.status == "BLOCKED" else "needs_changes"
    packet_response = await client.create_review_packet(_review_packet_payload(result, settings, review_status=review_status))
    review_packet_id = _response_identifier(packet_response)
    log_event(
        "agent_bus_runtime_bridge_review_packet_created",
        **context,
        review_packet_id=review_packet_id,
        review_status=review_status,
    )
    await client.attach_review_to_work_item(
        result.work_item_id or "",
        _review_attachment_payload(result, review_packet_id, packet_response, review_status=review_status),
    )


def _transition_sequence(status: str | None) -> list[str]:
    if status == "COMPLETED":
        return []
    if status == "APPROVED":
        return []
    if status == "REVIEW_IN_PROGRESS":
        return ["APPROVED"]
    if status == "READY_FOR_REVIEW":
        return ["REVIEW_IN_PROGRESS", "APPROVED"]
    if status in {"IN_PROGRESS", "AWAITING_EVIDENCE"}:
        return ["READY_FOR_REVIEW", "REVIEW_IN_PROGRESS", "APPROVED"]
    if status == "CLAIMED":
        return ["IN_PROGRESS", "READY_FOR_REVIEW", "REVIEW_IN_PROGRESS", "APPROVED"]
    if status == "QUEUED":
        return ["CLAIMED", "IN_PROGRESS", "READY_FOR_REVIEW", "REVIEW_IN_PROGRESS", "APPROVED"]
    return []


def _review_packet_payload(result: RuntimeValidationResult, settings: Settings, *, review_status: str) -> dict[str, Any]:
    approved = review_status == "approved"
    return _compact(
        {
            "work_item_id": result.work_item_id,
            "reviewer": _reviewer(result, settings),
            "reviewer_agent": _reviewer(result, settings),
            "review_status": review_status,
            "decision": review_status,
            "summary": _review_summary(result, approved=approved),
            "findings": [] if approved else [_failure_finding(result)],
            "required_changes": [] if approved else [_failure_finding(result)],
            "evidence_packet_ids_reviewed": [result.evidence_id] if result.evidence_id else [],
            "artifacts": result.evidence.artifacts,
            "metadata": _bridge_metadata(result),
            "risk_level": "low" if approved else "medium",
            "review_type": "runtime_validation",
            "verified": ["Hermes Playwright runtime validation passed."] if approved else [],
            "assumed": [],
            "unverified": [] if approved else ["Hermes Playwright runtime validation did not pass."],
            "commands_run": [],
            "test_results": [
                _compact(
                    {
                        "name": "Hermes Playwright runtime validation",
                        "status": result.hermes.status,
                        "job_id": result.hermes.job_id,
                        "target_url": result.hermes.target_url,
                    }
                )
            ],
            "urls": [result.hermes.target_url] if result.hermes.target_url else [],
        }
    )


def _review_attachment_payload(
    result: RuntimeValidationResult,
    review_packet_id: str | None,
    packet_response: dict[str, Any],
    *,
    review_status: str,
) -> dict[str, Any]:
    return _compact(
        {
            "review_packet_id": review_packet_id,
            "review_packet": packet_response,
            "reviewer": result.review_agent or result.review_dispatch.get("review_agent") or "bb2",
            "review_status": review_status,
            "decision": review_status,
            "metadata": _bridge_metadata(result),
        }
    )


def _bridge_context(result: RuntimeValidationResult, settings: Settings) -> dict[str, Any]:
    return {
        "workflow_id": result.workflow_id,
        "runtime_validation_id": result.validation_id,
        "work_item_id": result.work_item_id,
        "repository": result.repo,
        "branch": result.branch,
        "commit_sha": result.review_dispatch.get("commit_sha"),
        "pr_number": result.pr_number,
        "execution_type": result.review_dispatch.get("execution_type") or result.validation_type,
        "assigned_agent": _reviewer(result, settings),
        "dispatch_reason": "terminal_runtime_validation_result",
        "evidence_packet_id": result.evidence_id,
        "hermes_job_id": result.hermes.job_id,
        "terminal_status": result.hermes.status,
    }


def _bridge_metadata(result: RuntimeValidationResult) -> dict[str, Any]:
    return _compact(
        {
            "source": "runtime_validation_agent_bus_bridge",
            "approval_scope": "agent_bus_workflow_progression_only",
            "merge_authorization": False,
            "approval_boundary": (
                "Hermes PASSED satisfies runtime-validation evidence for Agent Bus WorkItem progression; "
                "it does not authorize PR merge or bypass BB2/human code-review policy."
            ),
            "workflow_id": result.workflow_id,
            "runtime_validation_id": result.validation_id,
            "work_item_id": result.work_item_id,
            "repository": result.repo,
            "pr_number": result.pr_number,
            "branch": result.branch,
            "base_branch": result.base_branch,
            "commit_sha": result.review_dispatch.get("commit_sha"),
            "evidence_packet_id": result.evidence_id,
            "hermes_job_id": result.hermes.job_id,
            "hermes_status": result.hermes.status,
            "runtime_validation_status": result.status,
            "target_url": result.hermes.target_url,
            "review_dispatch": result.review_dispatch,
        }
    )


def _hermes_passed(result: RuntimeValidationResult) -> bool:
    return result.status == "completed" and result.hermes.status == _PASSING_HERMES_STATUS


def _work_item_status(work_item: dict[str, Any]) -> str | None:
    for key in ("status", "state", "current_status", "currentStatus"):
        value = work_item.get(key)
        if value:
            return str(value).upper()
    return None


def _reviewer(result: RuntimeValidationResult, settings: Settings) -> str:
    return result.review_agent or result.review_dispatch.get("review_agent") or settings.agent_bus_review_agent or "bb2"


def _review_summary(result: RuntimeValidationResult, *, approved: bool) -> str:
    if approved:
        return "Hermes Playwright runtime validation passed."
    return f"Hermes Playwright runtime validation did not pass: {_failure_finding(result)}"


def _failure_finding(result: RuntimeValidationResult) -> str:
    return result.error or result.hermes.error or f"Hermes status: {result.hermes.status}."


def _response_identifier(response: dict[str, Any]) -> str | None:
    for key in ("review_packet_id", "packet_id", "id"):
        value = response.get(key)
        if value:
            return str(value)
    return None


def _log_skipped(context: dict[str, Any], *, reason: str, **extra: Any) -> None:
    log_event("agent_bus_runtime_bridge_skipped", **context, skip_reason=reason, **extra)


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
