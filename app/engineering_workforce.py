from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.agent_tasks import AgentTask
from app.config import Settings


logger = logging.getLogger("riseos_agent_orchestrator")

CODEX_M2 = "codex-m2"
CIRCUIT_FORGE = "circuit-forge"
SCHEDULER_METADATA_KEY = "_engineering_workforce_scheduler"

AvailabilityState = Literal["available", "busy", "unknown", "unavailable"]

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
DOCS_KEYWORDS = {"docs", "documentation", "readme", "runbook", "guide", "changelog"}
CIRCUIT_FRONTEND_ALLOW_KEYS = {"allow_circuit_frontend", "circuit_frontend_allowed", "allow_frontend_for_circuit"}
PREFERRED_AGENT_KEYS = {"preferred_agent", "preferred_engineering_worker", "scheduler_preferred_agent"}
REPO_PROFILE_KEYS = {"repo_profile", "repository_profile", "repo_type", "project_type"}


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
    availability_state: AvailabilityState = "unknown"
    capabilities: list[str] = field(default_factory=lambda: sorted(ENGINEERING_CAPABILITIES))
    busy: bool = False
    disabled: bool = False
    eligible: bool = True

    def as_metadata(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "available": self.available,
            "availability_state": self.availability_state,
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
    target_agent_explicit: bool = True
    candidates: list[WorkerCandidate] = field(default_factory=list)

    def metadata(self) -> dict[str, Any]:
        return {
            "scheduler_mode": self.scheduler_mode,
            "scheduler_selected_agent": self.selected_agent,
            "scheduler_reason": self.reason,
            "scheduler_candidates": [candidate.as_metadata() for candidate in self.candidates],
            "original_target_agent": self.original_target_agent,
            "target_agent_explicit": self.target_agent_explicit,
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
    target_agent_explicit = _target_agent_explicit(task)
    if not settings.enable_engineering_workforce_scheduler:
        logger.info("[WORKFORCE] scheduler disabled, preserving explicit target_agent task_id=%s target_agent=%s", task.task_id, original_target)
        return SchedulerDecision(
            scheduler_enabled=False,
            scheduler_mode=False,
            applied=False,
            original_target_agent=original_target,
            target_agent_explicit=target_agent_explicit,
            reason="scheduler_disabled",
        )

    normalized_target = normalize_engineering_agent(original_target)
    if target_agent_explicit and normalized_target in {CODEX_M2, CIRCUIT_FORGE}:
        if normalized_target != original_target:
            logger.info(
                "[WORKFORCE] canonicalized explicit target_agent task_id=%s original=%s canonical=%s",
                task.task_id,
                original_target,
                normalized_target,
            )
            return SchedulerDecision(
                scheduler_enabled=True,
                scheduler_mode=False,
                applied=True,
                selected_agent=normalized_target,
                original_target_agent=original_target,
                target_agent_explicit=True,
                reason="canonicalized_explicit_target_agent",
            )
        logger.info("[WORKFORCE] explicit target_agent preserved task_id=%s target_agent=%s", task.task_id, original_target)
        return SchedulerDecision(
            scheduler_enabled=True,
            scheduler_mode=False,
            applied=False,
            selected_agent=normalized_target,
            original_target_agent=original_target,
            target_agent_explicit=True,
            reason="explicit_target_agent",
        )

    if target_agent_explicit and normalized_target not in GENERIC_ENGINEERING_TARGETS:
        logger.info("[WORKFORCE] target_agent not schedulable task_id=%s target_agent=%s", task.task_id, original_target)
        return SchedulerDecision(
            scheduler_enabled=True,
            scheduler_mode=False,
            applied=False,
            original_target_agent=original_target,
            target_agent_explicit=True,
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
            candidate.availability_state,
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
        target_agent_explicit=target_agent_explicit,
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
        return WorkerCandidate(agent_id=CODEX_M2, available=False, busy=False, disabled=True, availability_state="unavailable", reason="codex_disabled")

    status = await _maybe_get_agent_status(signal_client, CODEX_M2)
    queue = await _maybe_get_agent_queue(signal_client, CODEX_M2)
    if isinstance(status, dict) and status.get("_lookup_error"):
        return WorkerCandidate(agent_id=CODEX_M2, available=False, busy=False, availability_state="unknown", reason="codex_status_unknown")

    status_value = str(status.get("status", "")).lower() if status else ""
    availability = str(status.get("availability", "")).lower() if status else ""
    health = str(status.get("health_state", status.get("health", ""))).lower() if status else ""
    active_queue = _has_active_work(queue)

    if status_value in {"offline", "unavailable", "blocked"}:
        return WorkerCandidate(agent_id=CODEX_M2, available=False, busy=False, availability_state="unavailable", reason=f"codex_status_{status_value}")
    if status_value == "busy" or availability in {"busy", "unavailable"}:
        return WorkerCandidate(agent_id=CODEX_M2, available=False, busy=True, availability_state="busy", reason="codex_busy")
    if health and health not in {"healthy", "ok", "available"}:
        return WorkerCandidate(agent_id=CODEX_M2, available=False, busy=False, availability_state="unavailable", reason=f"codex_health_{health}")
    if active_queue:
        return WorkerCandidate(agent_id=CODEX_M2, available=False, busy=True, availability_state="busy", reason="codex_queue_active")
    if status or queue is not None:
        return WorkerCandidate(agent_id=CODEX_M2, available=True, busy=False, availability_state="available", reason="codex_signals_available")
    return WorkerCandidate(agent_id=CODEX_M2, available=True, busy=False, availability_state="available", reason="codex_no_signals_assumed_available")


def _circuit_candidate(task: AgentTask, settings: Settings) -> WorkerCandidate:
    if not settings.circuit_engineering_worker_enabled:
        return WorkerCandidate(agent_id=CIRCUIT_FORGE, available=False, disabled=True, eligible=False, availability_state="unavailable", reason="circuit_disabled")
    if _repo_profile(task) == "frontend" and not _circuit_frontend_allowed(task):
        return WorkerCandidate(agent_id=CIRCUIT_FORGE, available=False, eligible=False, availability_state="unavailable", reason="frontend_repo_not_allowed_for_circuit")
    if _is_frontend_task(task) and not _circuit_frontend_allowed(task):
        return WorkerCandidate(agent_id=CIRCUIT_FORGE, available=False, eligible=False, availability_state="unavailable", reason="frontend_not_allowed_for_circuit")
    return WorkerCandidate(agent_id=CIRCUIT_FORGE, available=True, availability_state="available", reason="circuit_default_available")


def _select_candidate(
    task: AgentTask,
    candidates: list[WorkerCandidate],
    preferred: str | None,
) -> tuple[WorkerCandidate, str]:
    by_agent = {candidate.agent_id: candidate for candidate in candidates}
    codex = by_agent[CODEX_M2]
    circuit = by_agent[CIRCUIT_FORGE]

    if preferred:
        candidate = by_agent.get(preferred)
        if candidate and candidate.available and candidate.eligible:
            return candidate, f"metadata_preferred_{preferred}"

    if codex.available and codex.eligible:
        return codex, "preferred_available"
    if codex.availability_state == "unknown":
        return codex, "codex_unknown_preserved"
    if circuit.available and circuit.eligible:
        return circuit, codex.reason or "codex_unavailable"
    if _repo_profile(task) == "frontend" or (_is_frontend_task(task) and not circuit.eligible):
        return codex, "frontend_requires_codex"
    return codex, "no_available_fallback_codex"


async def _maybe_get_agent_status(signal_client: object | None, agent_id: str) -> dict[str, Any] | None:
    method = getattr(signal_client, "get_agent_status", None)
    if method is None:
        return None
    try:
        value = await method(agent_id)
    except Exception as exc:
        logger.warning("[WORKFORCE] candidate %s availability=unknown reason=status_lookup_failed error_type=%s", agent_id, type(exc).__name__)
        return {"_lookup_error": type(exc).__name__}
    return value if isinstance(value, dict) else None


async def _maybe_get_agent_queue(signal_client: object | None, agent_id: str) -> list[dict[str, Any]] | None:
    method = getattr(signal_client, "get_agent_queue", None)
    if method is None:
        return None
    try:
        value = await method(agent_id)
    except Exception as exc:
        logger.warning("[WORKFORCE] candidate %s queue_lookup_failed error_type=%s", agent_id, type(exc).__name__)
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
    values = _task_values(task)
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


def _target_agent_explicit(task: AgentTask) -> bool:
    values = _task_values(task)
    explicit = values.get("target_agent_explicit")
    return explicit if isinstance(explicit, bool) else True


def _repo_profile(task: AgentTask) -> str:
    values = _task_values(task)
    for key in REPO_PROFILE_KEYS:
        value = str(values.get(key, "")).strip().lower().replace("_", "-")
        if value in {"frontend", "front-end", "ui", "client"}:
            return "frontend"
        if value in {"backend", "api", "service", "server"}:
            return "backend"
        if value in {"docs", "documentation"}:
            return "docs"
    if _is_documentation_task(task):
        return "docs"
    return "general"


def _is_frontend_task(task: AgentTask) -> bool:
    text = " ".join([task.title, task.objective, task.body or "", " ".join(task.labels), " ".join(task.instructions)]).lower()
    return any(keyword in text for keyword in FRONTEND_KEYWORDS)


def _is_documentation_task(task: AgentTask) -> bool:
    text = " ".join([task.title, task.objective, task.body or "", " ".join(task.labels), " ".join(task.instructions)]).lower()
    return any(keyword in text for keyword in DOCS_KEYWORDS)


def _circuit_frontend_allowed(task: AgentTask) -> bool:
    values = _task_values(task)
    if any(bool(values.get(key)) for key in CIRCUIT_FRONTEND_ALLOW_KEYS):
        return True
    labels = {label.strip().lower() for label in task.labels}
    return bool(labels.intersection({"allow-circuit-frontend", "circuit-frontend-ok", "circuit-frontend-allowed"}))


def _task_values(task: AgentTask) -> dict[str, Any]:
    evidence = task.execution_evidence if isinstance(task.execution_evidence, dict) else {}
    routing = evidence.get("_routing") if isinstance(evidence.get("_routing"), dict) else {}
    return {**routing, **evidence}
