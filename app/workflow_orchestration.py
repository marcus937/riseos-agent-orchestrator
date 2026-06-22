from __future__ import annotations

import json
import sqlite3
from collections import deque
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from app.agent_tasks import (
    AgentTask,
    AgentTaskCreateRequest,
    AgentTaskPriority,
    AgentTaskStatus,
    AgentTaskStore,
    create_agent_task,
    refresh_agent_task_dependency_states,
)


class WorkflowStatus(StrEnum):
    CREATED = "created"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowPRStrategy(StrEnum):
    PER_TASK = "per_task"
    SHARED_WORKFLOW_BRANCH = "shared_workflow_branch"


class WorkflowTask(BaseModel):
    task_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    objective: str | None = None
    body: str | None = None
    repo_full_name: str | None = None
    issue_number: int | None = None
    labels: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    target_agent: str = "codex-m2"
    priority: AgentTaskPriority = AgentTaskPriority.NORMAL
    depends_on: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_objective_or_body(self) -> "WorkflowTask":
        if not (self.objective and self.objective.strip()) and not (self.body and self.body.strip()):
            raise ValueError("Either objective or body is required.")
        return self


class WorkflowCreateRequest(BaseModel):
    repo_full_name: str = Field(min_length=1)
    title: str = Field(min_length=1)
    correlation_id: str | None = None
    base_branch: str = "agent-integration"
    pr_strategy: WorkflowPRStrategy = WorkflowPRStrategy.PER_TASK
    tasks: list[WorkflowTask] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dependency_graph(self) -> "WorkflowCreateRequest":
        keys = [task.task_key for task in self.tasks]
        if len(keys) != len(set(keys)):
            raise ValueError("Workflow task_key values must be unique.")
        known = set(keys)
        for task in self.tasks:
            missing = [dependency for dependency in task.depends_on if dependency not in known]
            if missing:
                raise ValueError(f"Task {task.task_key} depends on unknown task_key values: {missing}.")
            if task.task_key in task.depends_on:
                raise ValueError(f"Task {task.task_key} cannot depend on itself.")
        _raise_for_cycles({task.task_key: task.depends_on for task in self.tasks})
        return self


class WorkflowTaskState(BaseModel):
    task_key: str
    task_id: str
    title: str
    status: AgentTaskStatus
    target_agent: str
    depends_on: list[str] = Field(default_factory=list)
    dependency_task_ids: list[str] = Field(default_factory=list)
    blocked: bool = False
    blocked_by: list[str] = Field(default_factory=list)
    agent_bus_work_item_id: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    failure: str | None = None


class WorkflowDependencyState(BaseModel):
    task_key: str
    depends_on: list[str] = Field(default_factory=list)
    dependency_task_ids: list[str] = Field(default_factory=list)
    satisfied: bool = False
    blocked_by: list[str] = Field(default_factory=list)


class WorkflowTimelineEvent(BaseModel):
    event: str
    occurred_at: datetime
    task_key: str | None = None
    task_id: str | None = None
    actor: str = "orchestrator"
    metadata: dict[str, Any] = Field(default_factory=dict)


class StoredWorkflow(BaseModel):
    workflow_id: str
    title: str
    repo_full_name: str
    correlation_id: str | None = None
    base_branch: str = "agent-integration"
    pr_strategy: WorkflowPRStrategy = WorkflowPRStrategy.PER_TASK
    shared_branch: str | None = None
    shared_pr_number: int | None = None
    task_key_to_task_id: dict[str, str] = Field(default_factory=dict)
    task_dependencies: dict[str, list[str]] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    timeline: list[WorkflowTimelineEvent] = Field(default_factory=list)


class WorkflowResponse(BaseModel):
    workflow_id: str
    title: str
    repo_full_name: str
    correlation_id: str | None = None
    status: WorkflowStatus
    base_branch: str = "agent-integration"
    pr_strategy: WorkflowPRStrategy = WorkflowPRStrategy.PER_TASK
    shared_branch: str | None = None
    shared_pr_number: int | None = None
    created_at: datetime
    updated_at: datetime
    task_statuses: list[WorkflowTaskState] = Field(default_factory=list)
    dependency_graph: list[WorkflowDependencyState] = Field(default_factory=list)
    current_running_task: WorkflowTaskState | None = None
    completed_tasks: list[str] = Field(default_factory=list)
    task_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    execution_timeline: list[WorkflowTimelineEvent] = Field(default_factory=list)


class WorkflowStore(Protocol):
    def save_workflow(self, workflow: StoredWorkflow) -> None: ...
    def get_workflow(self, workflow_id: str) -> StoredWorkflow | None: ...
    def list_workflows(self) -> list[StoredWorkflow]: ...


class InMemoryWorkflowStore:
    def __init__(self, max_items: int = 1000) -> None:
        self._items: deque[StoredWorkflow] = deque(maxlen=max_items)

    def save_workflow(self, workflow: StoredWorkflow) -> None:
        for index, existing in enumerate(self._items):
            if existing.workflow_id == workflow.workflow_id:
                self._items[index] = workflow
                return
        self._items.append(workflow)

    def get_workflow(self, workflow_id: str) -> StoredWorkflow | None:
        return next((workflow for workflow in self._items if workflow.workflow_id == workflow_id), None)

    def list_workflows(self) -> list[StoredWorkflow]:
        return sorted(self._items, key=lambda workflow: workflow.created_at, reverse=True)


class SQLiteWorkflowStore:
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
                CREATE TABLE IF NOT EXISTS workflows_v1 (
                    workflow_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def save_workflow(self, workflow: StoredWorkflow) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO workflows_v1 (
                    workflow_id,
                    payload,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (workflow.workflow_id, workflow.model_dump_json(), workflow.created_at.isoformat(), workflow.updated_at.isoformat()),
            )

    def get_workflow(self, workflow_id: str) -> StoredWorkflow | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM workflows_v1 WHERE workflow_id = ?", (workflow_id,)).fetchone()
        return _workflow_from_payload(row["payload"]) if row else None

    def list_workflows(self) -> list[StoredWorkflow]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM workflows_v1 ORDER BY created_at DESC").fetchall()
        return [_workflow_from_payload(row["payload"]) for row in rows]


workflow_store = InMemoryWorkflowStore()


def build_workflow_store(db_path: str | None) -> WorkflowStore:
    if not db_path:
        return workflow_store
    try:
        return SQLiteWorkflowStore(db_path)
    except (OSError, sqlite3.Error):
        return workflow_store


def create_workflow(request: WorkflowCreateRequest, *, workflow_store: WorkflowStore, agent_task_store: AgentTaskStore) -> StoredWorkflow:
    now = datetime.now(UTC)
    workflow_id = f"wf-{uuid4()}"
    shared_branch = _shared_branch_for_workflow(workflow_id) if request.pr_strategy == WorkflowPRStrategy.SHARED_WORKFLOW_BRANCH else None
    workflow = StoredWorkflow(
        workflow_id=workflow_id,
        title=request.title,
        repo_full_name=request.repo_full_name,
        correlation_id=request.correlation_id or workflow_id,
        base_branch=request.base_branch,
        pr_strategy=request.pr_strategy,
        shared_branch=shared_branch,
        created_at=now,
        updated_at=now,
        timeline=[WorkflowTimelineEvent(event="workflow_created", occurred_at=now, metadata={"task_count": len(request.tasks), "pr_strategy": request.pr_strategy.value})],
    )

    created_tasks: dict[str, AgentTask] = {}
    for workflow_task in request.tasks:
        agent_task = create_agent_task(
            AgentTaskCreateRequest(
                repo_full_name=workflow_task.repo_full_name or request.repo_full_name,
                title=workflow_task.title,
                objective=workflow_task.objective,
                body=workflow_task.body,
                issue_number=workflow_task.issue_number,
                labels=workflow_task.labels,
                instructions=workflow_task.instructions,
                acceptance_criteria=workflow_task.acceptance_criteria,
                target_agent=workflow_task.target_agent,
                priority=workflow_task.priority,
                correlation_id=workflow.workflow_id,
                dependency_task_ids=[],
            )
        )
        agent_task.source = "workflow_api"
        agent_task.execution_evidence = {"_routing": _workflow_routing(workflow)}
        created_tasks[workflow_task.task_key] = agent_task
        workflow.task_key_to_task_id[workflow_task.task_key] = agent_task.task_id
        workflow.task_dependencies[workflow_task.task_key] = _unique_keys(workflow_task.depends_on)

    for workflow_task in request.tasks:
        agent_task = created_tasks[workflow_task.task_key]
        agent_task.dependency_task_ids = [workflow.task_key_to_task_id[key] for key in workflow.task_dependencies[workflow_task.task_key]]
        agent_task_store.save_agent_task(agent_task)
        workflow.timeline.append(
            WorkflowTimelineEvent(
                event="task_created",
                occurred_at=agent_task.created_at,
                task_key=workflow_task.task_key,
                task_id=agent_task.task_id,
                metadata={"depends_on": workflow.task_dependencies[workflow_task.task_key]},
            )
        )

    refreshed = refresh_agent_task_dependency_states(list(created_tasks.values()))
    for task in refreshed:
        agent_task_store.save_agent_task(task)

    workflow.updated_at = datetime.now(UTC)
    workflow_store.save_workflow(workflow)
    return workflow


def build_workflow_response(workflow: StoredWorkflow, agent_tasks: list[AgentTask]) -> WorkflowResponse:
    tasks_by_id = {task.task_id: task for task in refresh_agent_task_dependency_states(agent_tasks)}
    task_states = [_task_state(task_key, workflow, tasks_by_id) for task_key in workflow.task_key_to_task_id]
    status = _workflow_status(task_states)
    timeline = _workflow_timeline(workflow, task_states, tasks_by_id)
    running = next((task for task in task_states if task.status in {AgentTaskStatus.CLAIMED, AgentTaskStatus.RUNNING, AgentTaskStatus.IN_PROGRESS, AgentTaskStatus.ASSIGNED}), None)
    completed = [task.task_key for task in task_states if task.status == AgentTaskStatus.COMPLETED]
    failures = [task.failure for task in task_states if task.failure]
    results = {task.task_key: task.result for task in task_states if task.result}
    updated_at = max([workflow.updated_at, *(event.occurred_at for event in timeline)], default=workflow.updated_at)
    shared_pr_number = workflow.shared_pr_number or _shared_pr_number(task_states)
    return WorkflowResponse(
        workflow_id=workflow.workflow_id,
        title=workflow.title,
        repo_full_name=workflow.repo_full_name,
        correlation_id=workflow.correlation_id,
        status=status,
        base_branch=workflow.base_branch,
        pr_strategy=workflow.pr_strategy,
        shared_branch=workflow.shared_branch,
        shared_pr_number=shared_pr_number,
        created_at=workflow.created_at,
        updated_at=updated_at,
        task_statuses=task_states,
        dependency_graph=[_dependency_state(task.task_key, workflow, tasks_by_id) for task in task_states],
        current_running_task=running,
        completed_tasks=completed,
        task_results=results,
        failures=failures,
        execution_timeline=timeline,
    )


def update_shared_workflow_routing_after_result(workflow: StoredWorkflow, agent_task_store: AgentTaskStore) -> None:
    if workflow.pr_strategy != WorkflowPRStrategy.SHARED_WORKFLOW_BRANCH:
        return
    tasks = refresh_agent_task_dependency_states(agent_task_store.list_agent_tasks())
    workflow_tasks = [task for task in tasks if task.correlation_id == workflow.workflow_id]
    pr_number = _shared_pr_number([_task_state(key, workflow, {task.task_id: task for task in workflow_tasks}) for key in workflow.task_key_to_task_id])
    if pr_number is None:
        return
    workflow.shared_pr_number = pr_number
    for task in workflow_tasks:
        if task.status != AgentTaskStatus.QUEUED:
            continue
        routing = _workflow_routing(workflow) | {"source_pr_number": pr_number}
        task.execution_evidence = {**task.execution_evidence, "_routing": routing}
        agent_task_store.save_agent_task(task)


def _task_state(task_key: str, workflow: StoredWorkflow, tasks_by_id: dict[str, AgentTask]) -> WorkflowTaskState:
    task_id = workflow.task_key_to_task_id[task_key]
    task = tasks_by_id.get(task_id)
    depends_on = workflow.task_dependencies.get(task_key, [])
    if task is None:
        return WorkflowTaskState(task_key=task_key, task_id=task_id, title="missing task", status=AgentTaskStatus.FAILED, target_agent="unknown", depends_on=depends_on, dependency_task_ids=[workflow.task_key_to_task_id[key] for key in depends_on if key in workflow.task_key_to_task_id], failure="Agent task record is missing.")
    result = {key: value for key, value in task.execution_evidence.items() if key != "_routing"}
    return WorkflowTaskState(
        task_key=task_key,
        task_id=task.task_id,
        title=task.title,
        status=task.status,
        target_agent=task.target_agent,
        depends_on=depends_on,
        dependency_task_ids=task.dependency_task_ids,
        blocked=task.blocked,
        blocked_by=task.blocked_by,
        agent_bus_work_item_id=task.agent_bus_work_item_id,
        branch=task.branch,
        commit_sha=task.commit_sha,
        changed_files=task.changed_files,
        result=result,
        failure=task.agent_bus_dispatch_error or (task.execution_evidence.get("error") if task.status == AgentTaskStatus.FAILED else None),
    )


def _dependency_state(task_key: str, workflow: StoredWorkflow, tasks_by_id: dict[str, AgentTask]) -> WorkflowDependencyState:
    depends_on = workflow.task_dependencies.get(task_key, [])
    dependency_task_ids = [workflow.task_key_to_task_id[key] for key in depends_on if key in workflow.task_key_to_task_id]
    blocked_by = [task_id for task_id in dependency_task_ids if tasks_by_id.get(task_id) is None or tasks_by_id[task_id].status != AgentTaskStatus.COMPLETED]
    return WorkflowDependencyState(task_key=task_key, depends_on=depends_on, dependency_task_ids=dependency_task_ids, satisfied=not blocked_by, blocked_by=blocked_by)


def _workflow_status(task_states: list[WorkflowTaskState]) -> WorkflowStatus:
    if not task_states:
        return WorkflowStatus.CREATED
    if all(task.status == AgentTaskStatus.COMPLETED for task in task_states):
        return WorkflowStatus.COMPLETED
    if any(task.status == AgentTaskStatus.CANCELLED for task in task_states) and not _has_runnable_or_active_task(task_states):
        return WorkflowStatus.CANCELLED
    if any(task.status == AgentTaskStatus.FAILED for task in task_states) and not _has_runnable_or_active_task(task_states):
        return WorkflowStatus.FAILED
    if _has_active_task(task_states):
        return WorkflowStatus.RUNNING
    if any(task.status == AgentTaskStatus.COMPLETED for task in task_states) and _has_runnable_task(task_states):
        return WorkflowStatus.RUNNING
    if _has_runnable_task(task_states):
        return WorkflowStatus.READY
    if any(task.blocked or task.status == AgentTaskStatus.FAILED for task in task_states):
        return WorkflowStatus.BLOCKED
    return WorkflowStatus.CREATED


def _has_runnable_or_active_task(task_states: list[WorkflowTaskState]) -> bool:
    return _has_active_task(task_states) or _has_runnable_task(task_states)


def _has_active_task(task_states: list[WorkflowTaskState]) -> bool:
    return any(task.status in {AgentTaskStatus.ASSIGNED, AgentTaskStatus.CLAIMED, AgentTaskStatus.RUNNING, AgentTaskStatus.IN_PROGRESS, AgentTaskStatus.READY_FOR_REVIEW} for task in task_states)


def _has_runnable_task(task_states: list[WorkflowTaskState]) -> bool:
    return any(task.status == AgentTaskStatus.QUEUED and not task.blocked and not task.failure for task in task_states)


def _workflow_timeline(workflow: StoredWorkflow, task_states: list[WorkflowTaskState], tasks_by_id: dict[str, AgentTask]) -> list[WorkflowTimelineEvent]:
    events = list(workflow.timeline)
    task_key_by_id = {state.task_id: state.task_key for state in task_states}
    for task_id, task in tasks_by_id.items():
        task_key = task_key_by_id.get(task_id)
        if task_key is None:
            continue
        for lifecycle_event in task.lifecycle_events:
            events.append(WorkflowTimelineEvent(event=f"task_{lifecycle_event.event}", occurred_at=lifecycle_event.occurred_at, task_key=task_key, task_id=task.task_id, actor=lifecycle_event.actor, metadata=lifecycle_event.metadata))
    return sorted(events, key=lambda event: event.occurred_at)


def _workflow_from_payload(payload: str) -> StoredWorkflow:
    return StoredWorkflow.model_validate(json.loads(payload))


def _workflow_routing(workflow: StoredWorkflow) -> dict[str, Any]:
    routing: dict[str, Any] = {"pr_strategy": workflow.pr_strategy.value, "base_branch": workflow.base_branch}
    if workflow.shared_branch:
        routing["source_branch"] = workflow.shared_branch
    if workflow.shared_pr_number:
        routing["source_pr_number"] = workflow.shared_pr_number
    return routing


def _shared_branch_for_workflow(workflow_id: str) -> str:
    return f"codex-m2/workflow-{_slugify(workflow_id)[:24]}"


def _shared_pr_number(task_states: list[WorkflowTaskState]) -> int | None:
    for state in task_states:
        pr = state.result.get("pull_request") if isinstance(state.result.get("pull_request"), dict) else None
        number = pr.get("number") if pr else None
        if isinstance(number, int):
            return number
        if isinstance(number, str) and number.isdigit():
            return int(number)
    return None


def _raise_for_cycles(graph: dict[str, list[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visited:
            return
        if key in visiting:
            raise ValueError("Workflow dependencies must not contain cycles.")
        visiting.add(key)
        for dependency in graph.get(key, []):
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in graph:
        visit(key)


def _unique_keys(keys: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for key in keys:
        normalized = key.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _slugify(value: str) -> str:
    return "-".join(part for part in value.lower().replace("_", "-").split("-") if part) or "workflow"
