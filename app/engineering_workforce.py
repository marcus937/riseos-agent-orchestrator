from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agent_tasks import AgentTask
from app.config import Settings


logger = logging.getLogger("riseos_agent_orchestrator")

CODEX_M2 = "codex-m2"
CIRCUIT_FORGE = "circuit-forge"
SCHEDULER_METADATA_KEY = "_engineering_workforce_scheduler"

ENGINEERING_WORKER_ALIASES = {
    "codex": CODEX_M2,
    "codex-m2": CODEX_M2,
    "circuit": CIRCUIT_FORGE,
    "circuit-forge": CIRCUIT_FORGE,
    "circuit forge": CIRCUIT_FORGE,
}
GENERIC_ENGINEERING_TARGETS = {"", "engineering", "coding", "engineer", "auto-engineer"}
ENGINEERING_CAPABILITIES = {"coding", "backend", "frontend", "docs", "github", "review_handoff"}
FRONTEND_KEYWORDS = {"frontend", "front-end", "ui", "ux", "react", "next.js", "browser", "css", "tailwind", "playwright"}
CIRCUIT_FRONTEND_ALLOW_KEYS = {"allow_circuit_frontend", "circuit_frontend_allowed", "allow_frontend_for_circuit"}
PREFERRED_AGENT_KEYS = {"preferred_agent", "preferred_engineering_worker", "scheduler_preferred_agent"}


class WorkforceSignalClient(Protocol):
    async def get_agent_status(self, agent_id: str) -> dict[str, Any]:
        ...

    async def get_agent_queue(self, agent_id: str) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True)
class WorkerCandidate:
    agent_id: str
    available: bool
    reason: str
    capabilities: list[str] = field(default_factory=lambda: sorted(ENGINEERING_CAPABILITIES))
    busy: bool = False
    disabled: bool = False
    eligible: bool = True

    def as_metadata(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "available": self.available,
            "busy": self.busy,
            "disabled": self.disabled,
            "eligible": self.eligible,
            "reason": self.reason,
            "capabilities": self.capabilities,
        }


@dataclass(frozen=True)
class SchedulerDecision:
    scheduler_enabled: bool
    scheduler_mode: bool
    applied: bool
    selected_agent: str | None = None
    reason: str | None = None
    original_target_agent: str | None = None
    candidates: list[WorkerCandidate] = field(default_factory=list)

    def metadata(self) -> dict[str, Any]:
        return {
            "scheduler_mode": self.scheduler_mode,
            "scheduler_selected_agent": self.selected_agent,
            "scheduler_reason": self.reason,
            "scheduler_candidates": [candidate.as_metadata() for candidate in self.candidates],
            "original_target_agent": self.original_target_agent,
        }


def normalize_engineering_agent(agent_name: str | None) -> str:
    normalized = (agent_name or "").strip().lower().replace("_", "-")
    return ENGINEERING_WORKER_ALIASES.get(normalized, normalized)


def is_generic_engineering_target(agent_name: str | None) -> bool:
    return normalize_engineering_agent(agent_name) in GENERIC_ENGINEERING_TARGETS


def scheduler_metadata_from_task(task: AgentTask) -> dict[str, Any]:
    evidence = task.execution_evidence if isinstance(task.execution_evidence, dict) else {}
    metadata = evidence.get(SCHEDULER_METADATA_KEY)
    return metadata if isinstance(metadata, dict) else {}


async def schedule_engineering_workforce(
    task: AgentTask,
    settings: Settings,
    *,
    signal_client: object | None = None,
) -> SchedulerDecision:
    original_target = task.target_agent
    if not settings.enable_engineering_workforce_scheduler:
        logger.info("[WORKFORCE] scheduler disabled, preserving explicit target_agent task_id=%s target_agent=%s", task.task_id, original_target)
        return SchedulerDecision(
            scheduler_enabled=False,
            scheduler_mode=False,
            applied=False,
            original_target_agent=original_target,
            reason="scheduler_disabled",
        )

    normalized_target = normalize_engineering_agent(original_target)
    if normalized_target in {CODEX_M2, CIRCUIT_FORGE}:
        logger.info("[WORKFORCE] explicit target_agent preserved task_id=%s target_agent=%s", task.task_id, original_target)
        return SchedulerDecision(
            scheduler_enabled=True,
            scheduler_mode=False,
            applied=False,
            selected_agent=normalized_target,
            original_target_agent=original_target,
            reason="explicit_target_agent",
        )

    if normalized_target not in GENERIC_ENGINEERING_TARGETS:
        logger.info("[WORKFORCE] target_agent not schedulable task_id=%s target_agent=%s", task.task_id, original_target)
        return SchedulerDecision(
            scheduler_enabled=True,
            scheduler_mode=False,
            applied=False,
            original_target_agent=original_target,
            reason="target_agent_not_engineering_generic",
        )

    logger.info("[WORKFORCE] scheduler evaluating task task_id=%s target_agent=%s", task.task_id, original_target)
    candidates = [
        await _codex_candidate(settings, signal_client),
        _circuit_candidate(task, settings),
    ]
    for candidate in candidates:
        logger.info(
            "[WORKFORCE] candidate %s availability=%s busy=%s eligible=%s reason=%s",
            candidate.agent_id,
            candidate.available,
            candidate.busy,
            candidate.eligible,
            candidate.reason,
        )

    preferred = _preferred_agent(task)
    selected, reason = _select_candidate(task, candidates, preferred)
    logger.info("[WORKFORCE] selected %s reason=%s task_id=%s", selected.agent_id, reason, task.task_id)
    return SchedulerDecision(
        scheduler_enabled=True,
        scheduler_mode=True,
        applied=True,
        selected_agent=selected.agent_id,
        reason=reason,
        original_target_agent=original_target,
        candidates=candidates,
    )


def apply_scheduler_decision(task: AgentTask, decision: SchedulerDecision) -> AgentTask:
    if not decision.applied or not decision.selected_agent:
        return task
    evidence = dict(task.execution_evidence if isinstance(task.execution_evidence, dict) else {})
    evidence[SCHEDULER_METADATA_KEY] = decision.metadata()
    task.execution_evidence = evidence
    task.target_agent = decision.selected_agent
    return task


async def _codex_candidate(settings: Settings, signal_client: object | None) -> WorkerCandidate:
    if not settings.codex_m2_engineering_worker_enabled:
        return WorkerCandidate(agent_id=CODEX_M2, available=False, busy=False, disabled=True, reason="codex_disabled")

    status = await _maybe_get_agent_status(signal_client, CODEX_M2)
    queue = await _maybe_get_agent_queue(signal_client, CODEX_M2)
    status_value = str(status.get("status", "")).lower() if status else ""
    availability = str(status.get("availability", "")).lower() if status else ""
    health = str(status.get("health_state", status.get("health", ""))).lower() if status else ""
    active_queue = _has_active_work(queue)

    if status_value in {"offline", "unavailable", "blocked"}:
        return WorkerCandidate(agent_id=CODEX_M2, available=False, busy=False, reason=f"codex_status_{status_value}")
    if status_value == "busy" or availability in {"busy", "unavailable"}:
        return WorkerCandidate(agent_id=CODEX_M2, available=False, busy=True, reason="codex_busy")
    if health and health not in {"healthy", "ok", "available"}:
        return WorkerCandidate(agent_id=CODEX_M2, available=False, busy=False, reason=f"codex_health_{health}")
    if active_queue:
        return WorkerCandidate(agent_id=CODEX_M2, available=False, busy=True, reason="codex_queue_active")
    if status or queue is not None:
        return WorkerCandidate(agent_id=CODEX_M2, available=True, busy=False, reason="codex_signals_available")
    return WorkerCandidate(agent_id=CODEX_M2, available=True, busy=False, reason="codex_no_signals_assumed_available")


def _circuit_candidate(task: AgentTask, settings: Settings) -> WorkerCandidate:
    if not settings.circuit_engineering_worker_enabled:
        return WorkerCandidate(agent_id=CIRCUIT_FORGE, available=False, disabled=True, eligible=False, reason="circuit_disabled")
    if _is_frontend_task(task) and not _circuit_frontend_allowed(task):
        return WorkerCandidate(agent_id=CIRCUIT_FORGE, available=False, eligible=False, reason="frontend_not_allowed_for_circuit")
    return WorkerCandidate(agent_id=CIRCUIT_FORGE, available=True, reason="circuit_default_available")


def _select_candidate(
    task: AgentTask,
    candidates: list[WorkerCandidate],
    preferred: str | None,
) -> tuple[WorkerCandidate, str]:
    by_agent = {candidate.agent_id: candidate for candidate in candidates}
    if preferred:
        candidate = by_agent.get(preferred)
        if candidate and candidate.available and candidate.eligible:
            return candidate, f"metadata_preferred_{preferred}"

    codex = by_agent[CODEX_M2]
    circuit = by_agent[CIRCUIT_FORGE]
    if codex.available and codex.eligible:
        return codex, "preferred_available"
    if circuit.available and circuit.eligible:
        return circuit, codex.reason or "codex_unavailable"
    if _is_frontend_task(task) and not circuit.eligible:
        return codex, "frontend_requires_codex"
    return codex, "no_available_fallback_codex"


async def _maybe_get_agent_status(signal_client: object | None, agent_id: str) -> dict[str, Any] | None:
    method = getattr(signal_client, "get_agent_status", None)
    if method is None:
        return None
    try:
        value = await method(agent_id)
    except Exception:
        return {"status": "unavailable"}
    return value if isinstance(value, dict) else None


async def _maybe_get_agent_queue(signal_client: object | None, agent_id: str) -> list[dict[str, Any]] | None:
    method = getattr(signal_client, "get_agent_queue", None)
    if method is None:
        return None
    try:
        value = await method(agent_id)
    except Exception:
        return None
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, dict)]


def _has_active_work(queue: list[dict[str, Any]] | None) -> bool:
    if not queue:
        return False
    inactive_statuses = {"completed", "failed", "cancelled", "rejected"}
    for item in queue:
        status = str(item.get("status", "")).lower()
        if status not in inactive_statuses:
            return True
    return False


def _preferred_agent(task: AgentTask) -> str | None:
    evidence = task.execution_evidence if isinstance(task.execution_evidence, dict) else {}
    routing = evidence.get("_routing") if isinstance(evidence.get("_routing"), dict) else {}
    values = {**routing, **evidence}
    for key in PREFERRED_AGENT_KEYS:
        preferred = normalize_engineering_agent(values.get(key))
        if preferred in {CODEX_M2, CIRCUIT_FORGE}:
            return preferred
    labels = {label.strip().lower() for label in task.labels}
    if labels.intersection({"prefer-circuit", "circuit-preferred", "circuit-forge-preferred"}):
        return CIRCUIT_FORGE
    if labels.intersection({"prefer-codex", "codex-preferred", "codex-m2-preferred"}):
        return CODEX_M2
    return None


def _is_frontend_task(task: AgentTask) -> bool:
    text = " ".join([task.title, task.objective, task.body or "", " ".join(task.labels), " ".join(task.instructions)]).lower()
    return any(keyword in text for keyword in FRONTEND_KEYWORDS)


def _circuit_frontend_allowed(task: AgentTask) -> bool:
    evidence = task.execution_evidence if isinstance(task.execution_evidence, dict) else {}
    routing = evidence.get("_routing") if isinstance(evidence.get("_routing"), dict) else {}
    values = {**routing, **evidence}
    if any(bool(values.get(key)) for key in CIRCUIT_FRONTEND_ALLOW_KEYS):
        return True
    labels = {label.strip().lower() for label in task.labels}
    return bool(labels.intersection({"allow-circuit-frontend", "circuit-frontend-ok", "circuit-frontend-allowed"}))
