from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationHermesSummary,
    RuntimeValidationRequest,
    RuntimeValidationResult,
)
from app.config import Settings
from app.operational_logging import log_event

READY_DEPLOYMENT_STATES = {"ready", "success"}


class WF20DeploymentState(StrEnum):
    WAITING_FOR_DEPLOYMENT = "WAITING_FOR_DEPLOYMENT"
    DEPLOYMENT_READY = "DEPLOYMENT_READY"


class WaitingWorkflow(BaseModel):
    repository: str
    branch: str | None = None
    pr_number: int | None = None
    commit_sha: str | None = None
    workflow_id: str
    correlation_id: str
    created_at: datetime
    state: WF20DeploymentState = WF20DeploymentState.WAITING_FOR_DEPLOYMENT
    preview_url: str | None = None
    deployment_id: str | None = None
    deployment_status_id: str | None = None
    resumed_at: datetime | None = None


class ResumeDecision(BaseModel):
    matched: bool = False
    claimed: bool = False
    already_resumed: bool = False
    workflow: WaitingWorkflow | None = None


class WF20DeploymentWaitStore:
    def save_waiting(self, workflow: WaitingWorkflow) -> WaitingWorkflow: ...
    def claim_ready(self, event: RuntimeValidationRequest) -> ResumeDecision: ...


class InMemoryWF20DeploymentWaitStore:
    def __init__(self) -> None:
        self._workflows: dict[str, WaitingWorkflow] = {}

    def save_waiting(self, workflow: WaitingWorkflow) -> WaitingWorkflow:
        existing = self._workflows.get(workflow.workflow_id)
        if existing and existing.state == WF20DeploymentState.DEPLOYMENT_READY:
            return existing
        self._workflows[workflow.workflow_id] = workflow
        return workflow

    def claim_ready(self, event: RuntimeValidationRequest) -> ResumeDecision:
        match = _best_match(list(self._workflows.values()), event)
        if match is None:
            return ResumeDecision()
        if match.state == WF20DeploymentState.DEPLOYMENT_READY:
            return ResumeDecision(matched=True, already_resumed=True, workflow=match)
        updated = match.model_copy(
            update={
                "state": WF20DeploymentState.DEPLOYMENT_READY,
                "preview_url": event.target_url,
                "deployment_id": _deployment_id(event),
                "deployment_status_id": _deployment_status_id(event),
                "resumed_at": datetime.now(UTC),
            }
        )
        self._workflows[updated.workflow_id] = updated
        return ResumeDecision(matched=True, claimed=True, workflow=updated)


class SQLiteWF20DeploymentWaitStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wf20_deployment_waits_v1 (
                    workflow_id TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    branch TEXT,
                    pr_number INTEGER,
                    commit_sha TEXT,
                    correlation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    preview_url TEXT,
                    deployment_id TEXT,
                    deployment_status_id TEXT,
                    resumed_at TEXT
                )
                """
            )

    def save_waiting(self, workflow: WaitingWorkflow) -> WaitingWorkflow:
        existing = self._get(workflow.workflow_id)
        if existing and existing.state == WF20DeploymentState.DEPLOYMENT_READY:
            return existing
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO wf20_deployment_waits_v1 (
                    workflow_id, repository, branch, pr_number, commit_sha, correlation_id,
                    created_at, state, preview_url, deployment_id, deployment_status_id, resumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _row_values(workflow),
            )
        return workflow

    def claim_ready(self, event: RuntimeValidationRequest) -> ResumeDecision:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM wf20_deployment_waits_v1 WHERE repository = ?", (event.repo,)).fetchall()
        match = _best_match([_workflow_from_row(row) for row in rows], event)
        if match is None:
            return ResumeDecision()
        if match.state == WF20DeploymentState.DEPLOYMENT_READY:
            return ResumeDecision(matched=True, already_resumed=True, workflow=match)
        updated = match.model_copy(
            update={
                "state": WF20DeploymentState.DEPLOYMENT_READY,
                "preview_url": event.target_url,
                "deployment_id": _deployment_id(event),
                "deployment_status_id": _deployment_status_id(event),
                "resumed_at": datetime.now(UTC),
            }
        )
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE wf20_deployment_waits_v1
                SET state = ?, preview_url = ?, deployment_id = ?, deployment_status_id = ?, resumed_at = ?
                WHERE workflow_id = ? AND state = ?
                """,
                (
                    updated.state.value,
                    updated.preview_url,
                    updated.deployment_id,
                    updated.deployment_status_id,
                    updated.resumed_at.isoformat() if updated.resumed_at else None,
                    updated.workflow_id,
                    WF20DeploymentState.WAITING_FOR_DEPLOYMENT.value,
                ),
            )
        if cursor.rowcount != 1:
            refreshed = self._get(updated.workflow_id) or updated
            return ResumeDecision(matched=True, already_resumed=True, workflow=refreshed)
        return ResumeDecision(matched=True, claimed=True, workflow=updated)

    def _get(self, workflow_id: str) -> WaitingWorkflow | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM wf20_deployment_waits_v1 WHERE workflow_id = ?", (workflow_id,)).fetchone()
        return _workflow_from_row(row) if row else None


_default_store: WF20DeploymentWaitStore = InMemoryWF20DeploymentWaitStore()
_original_trigger: Callable[..., Any] | None = None


def build_wf20_deployment_wait_store(db_path: str | None) -> WF20DeploymentWaitStore:
    if not db_path:
        return _default_store
    try:
        return SQLiteWF20DeploymentWaitStore(db_path)
    except (OSError, sqlite3.Error):
        return _default_store


def install_event_driven_wf20_runtime_validation(store: WF20DeploymentWaitStore | None = None) -> None:
    from app.wf20_runtime_validation import AgentBusRuntimeValidationStore

    global _original_trigger, _default_store
    if store is not None:
        _default_store = store
    if getattr(AgentBusRuntimeValidationStore, "_wf20_event_driven_installed", False):
        return
    _original_trigger = AgentBusRuntimeValidationStore.trigger

    async def trigger(self: Any, request: RuntimeValidationRequest, settings: Settings) -> RuntimeValidationResult:
        active_store = getattr(self, "_deployment_wait_store", None) or _default_store
        if _should_wait_for_deployment(request):
            workflow = _waiting_workflow_from_request(request)
            active_store.save_waiting(workflow)
            _log_waiting(workflow, request)
            return _pending_result(request, workflow)

        if _is_ready_deployment_resume(request):
            _log_deployment_status_received(request)
            decision = active_store.claim_ready(request)
            if not decision.matched:
                return _ignored_ready_deployment_result(request, "No waiting WF20 workflow matched the Ready deployment.")
            if decision.already_resumed:
                _log_workflow_already_resumed(decision.workflow, request)
                return _ignored_ready_deployment_result(request, "Matching WF20 workflow was already resumed.")
            _log_matched_waiting_workflow(decision.workflow, request)
            _log_resuming_workflow(decision.workflow, request)
            _log_starting_hermes(decision.workflow, request)

        return await _original_trigger(self, request, settings)  # type: ignore[misc]

    AgentBusRuntimeValidationStore.trigger = trigger  # type: ignore[method-assign]
    AgentBusRuntimeValidationStore._wf20_event_driven_installed = True  # type: ignore[attr-defined]


def attach_wf20_deployment_wait_store(runtime_store: Any, store: WF20DeploymentWaitStore) -> None:
    setattr(runtime_store, "_deployment_wait_store", store)


def _should_wait_for_deployment(request: RuntimeValidationRequest) -> bool:
    if _is_deployment_status_payload(request):
        return False
    readiness = str(getattr(request, "vercel_readiness", ""))
    return request.target_url is None and readiness in {"VERCEL_TIMEOUT", ""}


def _is_ready_deployment_resume(request: RuntimeValidationRequest) -> bool:
    if not _is_deployment_status_payload(request):
        return False
    if not request.target_url:
        return False
    return _deployment_state(request).lower() in READY_DEPLOYMENT_STATES


def _is_deployment_status_payload(request: RuntimeValidationRequest) -> bool:
    raw = getattr(request, "raw_github_event", None)
    return isinstance(raw, dict) and isinstance(raw.get("deployment"), dict) and isinstance(raw.get("deployment_status"), dict)


def _waiting_workflow_from_request(request: RuntimeValidationRequest) -> WaitingWorkflow:
    now = datetime.now(UTC)
    workflow_id = request.workflow_id or request.correlation_id or f"wf20-{request.repo}-{request.pr_number or request.branch or 'unknown'}"
    return WaitingWorkflow(
        repository=request.repo,
        branch=request.branch,
        pr_number=request.pr_number,
        commit_sha=str(getattr(request, "commit_sha", "") or "") or None,
        workflow_id=workflow_id,
        correlation_id=request.correlation_id or workflow_id,
        created_at=now,
    )


def _pending_result(request: RuntimeValidationRequest, workflow: WaitingWorkflow) -> RuntimeValidationResult:
    return RuntimeValidationResult(
        validation_id=f"wf20-waiting-{workflow.workflow_id}",
        status="pending",
        repo=request.repo,
        issue_number=request.issue_number,
        pr_number=request.pr_number,
        branch=request.branch,
        base_branch=request.base_branch,
        work_item_id=request.work_item_id,
        evidence_id=request.evidence_id,
        review_agent=request.review_agent,
        workflow_id=workflow.workflow_id,
        review_dispatch=request.review_dispatch,
        validation_type=request.validation_type,
        requested_by=request.requested_by,
        created_at=workflow.created_at,
        correlation_id=workflow.correlation_id,
        hermes=RuntimeValidationHermesSummary(target_url=None, target_source="deployment_status_webhook", status="SKIPPED", error="Waiting for Ready Vercel Preview deployment_status webhook."),
        evidence=RuntimeValidationEvidenceSummary(error="Waiting for Ready Vercel Preview deployment_status webhook."),
        bb2=RuntimeValidationBB2Packet(review_status="pending"),
        error="Waiting for Ready Vercel Preview deployment_status webhook.",
    )


def _ignored_ready_deployment_result(request: RuntimeValidationRequest, reason: str) -> RuntimeValidationResult:
    now = datetime.now(UTC)
    workflow_id = request.workflow_id or request.correlation_id or "wf20-unmatched-deployment"
    return RuntimeValidationResult(
        validation_id=f"wf20-ignored-{workflow_id}",
        status="pending",
        repo=request.repo,
        issue_number=request.issue_number,
        pr_number=request.pr_number,
        branch=request.branch,
        base_branch=request.base_branch,
        workflow_id=workflow_id,
        review_dispatch=request.review_dispatch,
        validation_type=request.validation_type,
        requested_by=request.requested_by,
        created_at=now,
        correlation_id=request.correlation_id or workflow_id,
        hermes=RuntimeValidationHermesSummary(target_url=request.target_url, target_source=request.target_url_source, status="SKIPPED", error=reason),
        evidence=RuntimeValidationEvidenceSummary(error=reason),
        bb2=RuntimeValidationBB2Packet(review_status="pending"),
        error=reason,
    )


def _best_match(workflows: list[WaitingWorkflow], event: RuntimeValidationRequest) -> WaitingWorkflow | None:
    repo_matches = [workflow for workflow in workflows if workflow.repository == event.repo]
    waiting = [workflow for workflow in repo_matches if workflow.state == WF20DeploymentState.WAITING_FOR_DEPLOYMENT]
    resumed = [workflow for workflow in repo_matches if workflow.state == WF20DeploymentState.DEPLOYMENT_READY]
    for pool in (waiting, resumed):
        match = _match_by_commit(pool, event)
        if match is not None:
            return match
        match = _match_by_pr(pool, event)
        if match is not None:
            return match
        match = _match_by_branch(pool, event)
        if match is not None:
            return match
    return None


def _match_by_commit(workflows: list[WaitingWorkflow], event: RuntimeValidationRequest) -> WaitingWorkflow | None:
    commit_sha = str(getattr(event, "commit_sha", "") or "")
    if not commit_sha:
        return None
    return next((workflow for workflow in workflows if workflow.commit_sha == commit_sha), None)


def _match_by_pr(workflows: list[WaitingWorkflow], event: RuntimeValidationRequest) -> WaitingWorkflow | None:
    if event.pr_number is None:
        return None
    return next((workflow for workflow in workflows if workflow.pr_number == event.pr_number), None)


def _match_by_branch(workflows: list[WaitingWorkflow], event: RuntimeValidationRequest) -> WaitingWorkflow | None:
    if not event.branch:
        return None
    return next((workflow for workflow in workflows if workflow.branch == event.branch), None)


def _row_values(workflow: WaitingWorkflow) -> tuple[Any, ...]:
    return (
        workflow.workflow_id,
        workflow.repository,
        workflow.branch,
        workflow.pr_number,
        workflow.commit_sha,
        workflow.correlation_id,
        workflow.created_at.isoformat(),
        workflow.state.value,
        workflow.preview_url,
        workflow.deployment_id,
        workflow.deployment_status_id,
        workflow.resumed_at.isoformat() if workflow.resumed_at else None,
    )


def _workflow_from_row(row: sqlite3.Row) -> WaitingWorkflow:
    return WaitingWorkflow(
        workflow_id=str(row["workflow_id"]),
        repository=str(row["repository"]),
        branch=row["branch"],
        pr_number=row["pr_number"],
        commit_sha=row["commit_sha"],
        correlation_id=str(row["correlation_id"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        state=WF20DeploymentState(str(row["state"])),
        preview_url=row["preview_url"],
        deployment_id=row["deployment_id"],
        deployment_status_id=row["deployment_status_id"],
        resumed_at=datetime.fromisoformat(row["resumed_at"]) if row["resumed_at"] else None,
    )


def _deployment_id(request: RuntimeValidationRequest) -> str | None:
    raw = getattr(request, "raw_github_event", None)
    deployment = raw.get("deployment") if isinstance(raw, dict) else None
    value = deployment.get("id") if isinstance(deployment, dict) else None
    return str(value) if value is not None else None


def _deployment_status_id(request: RuntimeValidationRequest) -> str | None:
    raw = getattr(request, "raw_github_event", None)
    deployment_status = raw.get("deployment_status") if isinstance(raw, dict) else None
    value = deployment_status.get("id") if isinstance(deployment_status, dict) else None
    return str(value) if value is not None else None


def _deployment_state(request: RuntimeValidationRequest) -> str:
    raw = getattr(request, "raw_github_event", None)
    deployment_status = raw.get("deployment_status") if isinstance(raw, dict) else None
    if isinstance(deployment_status, dict):
        return str(deployment_status.get("state") or "")
    return ""


def _log_fields(workflow: WaitingWorkflow | None, request: RuntimeValidationRequest) -> dict[str, Any]:
    return {
        "workflow_id": (workflow.workflow_id if workflow else request.workflow_id),
        "repo": request.repo,
        "pr": request.pr_number,
        "branch": request.branch,
        "commit_sha": str(getattr(request, "commit_sha", "") or (workflow.commit_sha if workflow else "") or "") or None,
        "deployment_id": _deployment_id(request),
        "deployment_status_id": _deployment_status_id(request),
        "preview_url": request.target_url,
    }


def _log_waiting(workflow: WaitingWorkflow, request: RuntimeValidationRequest) -> None:
    log_event("WAITING_FOR_DEPLOYMENT", **_log_fields(workflow, request))


def _log_deployment_status_received(request: RuntimeValidationRequest) -> None:
    log_event("DEPLOYMENT_STATUS_RECEIVED", **_log_fields(None, request))


def _log_matched_waiting_workflow(workflow: WaitingWorkflow | None, request: RuntimeValidationRequest) -> None:
    log_event("MATCHED_WAITING_WORKFLOW", **_log_fields(workflow, request))


def _log_resuming_workflow(workflow: WaitingWorkflow | None, request: RuntimeValidationRequest) -> None:
    log_event("RESUMING_WORKFLOW", **_log_fields(workflow, request))


def _log_starting_hermes(workflow: WaitingWorkflow | None, request: RuntimeValidationRequest) -> None:
    log_event("STARTING_HERMES", **_log_fields(workflow, request))


def _log_workflow_already_resumed(workflow: WaitingWorkflow | None, request: RuntimeValidationRequest) -> None:
    log_event("WORKFLOW_ALREADY_RESUMED", **_log_fields(workflow, request))
