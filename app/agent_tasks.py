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


class AgentTaskStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    ASSIGNED = "assigned"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    IN_PROGRESS = "in_progress"
    READY_FOR_REVIEW = "ready_for_review"


class AgentTaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class AgentTaskLifecycleEvent(BaseModel):
    event: str
    occurred_at: datetime
    actor: str = "orchestrator"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentTaskCreateRequest(BaseModel):
    repo_full_name: str = Field(min_length=1)
    title: str = Field(min_length=1)
    objective: str | None = None
    body: str | None = None
    issue_number: int | None = None
    labels: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    target_agent: str = "codex-m2"
    priority: AgentTaskPriority = AgentTaskPriority.NORMAL
    correlation_id: str | None = None
    dependency_task_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_objective_or_body(self) -> "AgentTaskCreateRequest":
        if not (self.objective and self.objective.strip()) and not (self.body and self.body.strip()):
            raise ValueError("Either objective or body is required.")
        return self


class AgentTaskCreateResponse(BaseModel):
    task_id: str
    status: AgentTaskStatus
    created_at: datetime
    target_agent: str
    agent_bus_work_item_id: str | None = None
    dependency_task_ids: list[str] = Field(default_factory=list)
    blocked: bool = False
    blocked_by: list[str] = Field(default_factory=list)


class AgentTaskExecutionResult(BaseModel):
    agent_id: str = Field(min_length=1)
    status: AgentTaskStatus
    commit_sha: str | None = None
    branch: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class AgentTask(BaseModel):
    task_id: str
    repo_full_name: str
    title: str
    objective: str
    body: str | None = None
    labels: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    target_agent: str
    priority: AgentTaskPriority = AgentTaskPriority.NORMAL
    correlation_id: str | None = None
    dependency_task_ids: list[str] = Field(default_factory=list)
    blocked: bool = False
    blocked_by: list[str] = Field(default_factory=list)
    status: AgentTaskStatus = AgentTaskStatus.CREATED
    source: str = "direct_api"
    issue_number: int | None = None
    agent_bus_work_item_id: str | None = None
    agent_bus_dispatch_error: str | None = None
    created_at: datetime
    updated_at: datetime
    queued_at: datetime | None = None
    assigned_at: datetime | None = None
    claimed_at: datetime | None = None
    running_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    cancelled_at: datetime | None = None
    branch: str | None = None
    commit_sha: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    execution_evidence: dict[str, Any] = Field(default_factory=dict)
    lifecycle_events: list[AgentTaskLifecycleEvent] = Field(default_factory=list)


class AgentTaskWorkflowSummary(BaseModel):
    task_id: str
    repo_full_name: str
    target_agent: str
    correlation_id: str | None = None
    status: AgentTaskStatus = AgentTaskStatus.CREATED
    issue_number: int | None = None
    agent_bus_work_item_id: str | None = None
    created_at: datetime
    updated_at: datetime
    queued_at: datetime | None = None
    assigned_at: datetime | None = None
    claimed_at: datetime | None = None
    running_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    cancelled_at: datetime | None = None
    branch: str | None = None
    commit_sha: str | None = None
    last_actor: str | None = None
    workflow_event_count: int = 1


class AgentTaskStore(Protocol):
    def save_agent_task(self, task: AgentTask) -> None:
        ...

    def list_agent_tasks(self) -> list[AgentTask]:
        ...

    def count_agent_tasks(self) -> int:
        ...

    def count_agent_tasks_for_workflow_collection(
        self,
        *,
        workflow_filter: str = "active_recent",
        recent_since: datetime | None = None,
    ) -> int:
        ...

    def list_agent_tasks_for_workflow_collection(
        self,
        *,
        limit: int,
        workflow_filter: str = "active_recent",
        recent_since: datetime | None = None,
    ) -> list[AgentTask]:
        ...

    def list_agent_task_workflow_summaries_for_collection(
        self,
        *,
        limit: int,
        workflow_filter: str = "active_recent",
        recent_since: datetime | None = None,
    ) -> list[AgentTaskWorkflowSummary]:
        ...

    def workflow_summary_counts_for_snapshot(self) -> "WorkflowSummaryCounts":
        ...

    def get_agent_task(self, task_id: str) -> AgentTask | None:
        ...


class InMemoryAgentTaskStore:
    def __init__(self, max_items: int = 1000) -> None:
        self._items: deque[AgentTask] = deque(maxlen=max_items)

    def save_agent_task(self, task: AgentTask) -> None:
        for index, existing in enumerate(self._items):
            if existing.task_id == task.task_id:
                self._items[index] = task
                return
        self._items.append(task)

    def list_agent_tasks(self) -> list[AgentTask]:
        return sorted(self._items, key=lambda task: task.created_at, reverse=True)

    def count_agent_tasks(self) -> int:
        return len(self._items)

    def count_agent_tasks_for_workflow_collection(
        self,
        *,
        workflow_filter: str = "active_recent",
        recent_since: datetime | None = None,
    ) -> int:
        refresh_agent_task_dependency_states(list(self._items))
        return len(
            _filter_agent_tasks_for_workflow_collection(
                list(self._items),
                workflow_filter=workflow_filter,
                recent_since=recent_since,
            )
        )

    def list_agent_tasks_for_workflow_collection(
        self,
        *,
        limit: int,
        workflow_filter: str = "active_recent",
        recent_since: datetime | None = None,
    ) -> list[AgentTask]:
        if limit <= 0:
            return []
        refresh_agent_task_dependency_states(list(self._items))
        tasks = _filter_agent_tasks_for_workflow_collection(
            list(self._items),
            workflow_filter=workflow_filter,
            recent_since=recent_since,
        )
        return _sort_agent_tasks_for_workflow_collection(tasks)[:limit]

    def list_agent_task_workflow_summaries_for_collection(
        self,
        *,
        limit: int,
        workflow_filter: str = "active_recent",
        recent_since: datetime | None = None,
    ) -> list[AgentTaskWorkflowSummary]:
        return [
            agent_task_workflow_summary(task)
            for task in self.list_agent_tasks_for_workflow_collection(
                limit=limit,
                workflow_filter=workflow_filter,
                recent_since=recent_since,
            )
        ]

    def workflow_summary_counts_for_snapshot(self) -> "WorkflowSummaryCounts":
        refresh_agent_task_dependency_states(list(self._items))
        return _agent_task_workflow_summary_counts(list(self._items))

    def get_agent_task(self, task_id: str) -> AgentTask | None:
        return next((task for task in self._items if task.task_id == task_id), None)

    def reset(self) -> None:
        self._items.clear()


class SQLiteAgentTaskStore:
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
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    repo_full_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    acceptance_criteria TEXT NOT NULL,
                    target_agent TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    correlation_id TEXT,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    issue_number INTEGER,
                    agent_bus_work_item_id TEXT,
                    agent_bus_dispatch_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    queued_at TEXT,
                    assigned_at TEXT,
                    claimed_at TEXT,
                    running_at TEXT,
                    completed_at TEXT,
                    failed_at TEXT,
                    cancelled_at TEXT,
                    branch TEXT,
                    commit_sha TEXT,
                    changed_files TEXT NOT NULL DEFAULT '[]',
                    execution_evidence TEXT NOT NULL DEFAULT '{}',
                    lifecycle_events TEXT NOT NULL,
                    lifecycle_event_count INTEGER NOT NULL DEFAULT 1,
                    last_actor TEXT
                )
                """
            )
            for column_name, column_type in _AGENT_TASK_EXTRA_COLUMNS:
                _ensure_column(conn, "agent_tasks", column_name, column_type)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_workflow_collection
                ON agent_tasks (status, updated_at DESC, created_at DESC, task_id)
                """
            )

    def save_agent_task(self, task: AgentTask) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_tasks (
                    task_id,
                    repo_full_name,
                    title,
                    objective,
                    body,
                    labels,
                    instructions,
                    acceptance_criteria,
                    target_agent,
                    priority,
                    correlation_id,
                    dependency_task_ids,
                    blocked,
                    blocked_by,
                    status,
                    source,
                    issue_number,
                    agent_bus_work_item_id,
                    agent_bus_dispatch_error,
                    created_at,
                    updated_at,
                    queued_at,
                    assigned_at,
                    claimed_at,
                    running_at,
                    completed_at,
                    failed_at,
                    cancelled_at,
                    branch,
                    commit_sha,
                    changed_files,
                    execution_evidence,
                    lifecycle_events,
                    lifecycle_event_count,
                    last_actor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.repo_full_name,
                    task.title,
                    task.objective,
                    task.body,
                    json.dumps(task.labels),
                    json.dumps(task.instructions),
                    json.dumps(task.acceptance_criteria),
                    task.target_agent,
                    task.priority.value,
                    task.correlation_id,
                    json.dumps(task.dependency_task_ids),
                    int(task.blocked),
                    json.dumps(task.blocked_by),
                    task.status.value,
                    task.source,
                    task.issue_number,
                    task.agent_bus_work_item_id,
                    task.agent_bus_dispatch_error,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                    _dt(task.queued_at),
                    _dt(task.assigned_at),
                    _dt(task.claimed_at),
                    _dt(task.running_at),
                    _dt(task.completed_at),
                    _dt(task.failed_at),
                    _dt(task.cancelled_at),
                    task.branch,
                    task.commit_sha,
                    json.dumps(task.changed_files),
                    json.dumps(task.execution_evidence),
                    json.dumps([event.model_dump(mode="json") for event in task.lifecycle_events]),
                    _agent_task_lifecycle_event_count(task),
                    _last_agent_task_lifecycle_actor(task),
                ),
            )

    def list_agent_tasks(self) -> list[AgentTask]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_tasks
                ORDER BY created_at DESC
                """
            ).fetchall()
        return refresh_agent_task_dependency_states([_task_from_row(row) for row in rows])

    def count_agent_tasks(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM agent_tasks").fetchone()
        return int(row["count"])

    def count_agent_tasks_for_workflow_collection(
        self,
        *,
        workflow_filter: str = "active_recent",
        recent_since: datetime | None = None,
    ) -> int:
        where_sql, params = _agent_task_workflow_filter_sql(workflow_filter, recent_since)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM agent_tasks
                {where_sql}
                """,
                params,
            ).fetchone()
        return int(row["count"])

    def list_agent_tasks_for_workflow_collection(
        self,
        *,
        limit: int,
        workflow_filter: str = "active_recent",
        recent_since: datetime | None = None,
    ) -> list[AgentTask]:
        if limit <= 0:
            return []
        where_sql, params = _agent_task_workflow_filter_sql(workflow_filter, recent_since)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM agent_tasks
                {where_sql}
                ORDER BY {_AGENT_TASK_WORKFLOW_LAST_ACTIVITY_SQL} DESC,
                         updated_at DESC,
                         created_at DESC,
                         ('wf-agent-task-' || task_id) ASC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            tasks = [_task_from_row(row) for row in rows]
            return _refresh_sqlite_agent_task_dependency_states(conn, tasks)

    def list_agent_task_workflow_summaries_for_collection(
        self,
        *,
        limit: int,
        workflow_filter: str = "active_recent",
        recent_since: datetime | None = None,
    ) -> list[AgentTaskWorkflowSummary]:
        if limit <= 0:
            return []
        where_sql, params = _agent_task_workflow_filter_sql(workflow_filter, recent_since)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    task_id,
                    repo_full_name,
                    target_agent,
                    correlation_id,
                    status,
                    issue_number,
                    agent_bus_work_item_id,
                    created_at,
                    updated_at,
                    queued_at,
                    assigned_at,
                    claimed_at,
                    running_at,
                    completed_at,
                    failed_at,
                    cancelled_at,
                    branch,
                    commit_sha,
                    last_actor,
                    COALESCE(NULLIF(lifecycle_event_count, 0), 1) AS workflow_event_count
                FROM agent_tasks
                {where_sql}
                ORDER BY {_AGENT_TASK_WORKFLOW_LAST_ACTIVITY_SQL} DESC,
                         updated_at DESC,
                         created_at DESC,
                         ('wf-agent-task-' || task_id) ASC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [AgentTaskWorkflowSummary.model_validate(dict(row)) for row in rows]

    def workflow_summary_counts_for_snapshot(self) -> "WorkflowSummaryCounts":
        from app.workflows import WorkflowSummaryCounts

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status <> ? THEN 1 ELSE 0 END) AS active_count,
                    SUM(CASE WHEN status IN (?, ?) THEN 1 ELSE 0 END) AS blocked_count,
                    SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS reviewing_count
                FROM agent_tasks
                """,
                (
                    AgentTaskStatus.COMPLETED.value,
                    AgentTaskStatus.FAILED.value,
                    AgentTaskStatus.CANCELLED.value,
                    AgentTaskStatus.READY_FOR_REVIEW.value,
                ),
            ).fetchone()
        return WorkflowSummaryCounts(
            active=int(row["active_count"] or 0),
            blocked=int(row["blocked_count"] or 0),
            reviewing=int(row["reviewing_count"] or 0),
            verified=0,
        )

    def get_agent_task(self, task_id: str) -> AgentTask | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            task = _task_from_row(row)
            return _refresh_sqlite_agent_task_dependency_states(conn, [task])[0]


agent_task_store = InMemoryAgentTaskStore()


def create_agent_task(request: AgentTaskCreateRequest) -> AgentTask:
    now = datetime.now(UTC)
    task_id = f"agtask-{uuid4()}"
    objective = _task_objective(request)
    created = AgentTaskLifecycleEvent(event="created", occurred_at=now)
    queued = AgentTaskLifecycleEvent(
        event="queued",
        occurred_at=now,
        metadata={"target_agent": request.target_agent, "priority": request.priority.value},
    )
    return AgentTask(
        task_id=task_id,
        repo_full_name=request.repo_full_name,
        title=request.title,
        objective=objective,
        body=request.body,
        labels=request.labels,
        instructions=request.instructions,
        acceptance_criteria=request.acceptance_criteria,
        target_agent=request.target_agent,
        priority=request.priority,
        correlation_id=request.correlation_id,
        dependency_task_ids=_unique_task_ids(request.dependency_task_ids),
        status=AgentTaskStatus.QUEUED,
        source="direct_api",
        issue_number=request.issue_number,
        created_at=now,
        updated_at=now,
        queued_at=now,
        lifecycle_events=[created, queued],
    )


def refresh_agent_task_dependency_state(task: AgentTask, tasks_by_id: dict[str, AgentTask]) -> AgentTask:
    blocked_by = [task_id for task_id in task.dependency_task_ids if tasks_by_id.get(task_id) is None or tasks_by_id[task_id].status != AgentTaskStatus.COMPLETED]
    task.blocked_by = blocked_by
    task.blocked = bool(blocked_by)
    return task


def refresh_agent_task_dependency_states(tasks: list[AgentTask]) -> list[AgentTask]:
    tasks_by_id = {task.task_id: task for task in tasks}
    for task in tasks:
        refresh_agent_task_dependency_state(task, tasks_by_id)
    return tasks


def agent_task_workflow_summary(task: AgentTask) -> AgentTaskWorkflowSummary:
    return AgentTaskWorkflowSummary(
        task_id=task.task_id,
        repo_full_name=task.repo_full_name,
        target_agent=task.target_agent,
        correlation_id=task.correlation_id,
        status=task.status,
        issue_number=task.issue_number,
        agent_bus_work_item_id=task.agent_bus_work_item_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
        queued_at=task.queued_at,
        assigned_at=task.assigned_at,
        claimed_at=task.claimed_at,
        running_at=task.running_at,
        completed_at=task.completed_at,
        failed_at=task.failed_at,
        cancelled_at=task.cancelled_at,
        branch=task.branch,
        commit_sha=task.commit_sha,
        last_actor=_last_agent_task_lifecycle_actor(task),
        workflow_event_count=_agent_task_lifecycle_event_count(task),
    )


def _agent_task_workflow_summary_counts(tasks: list[AgentTask]) -> "WorkflowSummaryCounts":
    from app.workflows import WorkflowSummaryCounts

    return WorkflowSummaryCounts(
        active=sum(1 for task in tasks if task.status != AgentTaskStatus.COMPLETED),
        blocked=sum(1 for task in tasks if task.status in {AgentTaskStatus.FAILED, AgentTaskStatus.CANCELLED}),
        reviewing=sum(1 for task in tasks if task.status == AgentTaskStatus.READY_FOR_REVIEW),
        verified=0,
    )


def _refresh_sqlite_agent_task_dependency_states(
    conn: sqlite3.Connection,
    tasks: list[AgentTask],
) -> list[AgentTask]:
    tasks_by_id = {task.task_id: task for task in tasks}
    missing_dependency_ids = sorted(
        {
            dependency_id
            for task in tasks
            for dependency_id in task.dependency_task_ids
            if dependency_id not in tasks_by_id
        }
    )
    if missing_dependency_ids:
        rows = conn.execute(
            f"""
            SELECT *
            FROM agent_tasks
            WHERE task_id IN ({','.join('?' for _ in missing_dependency_ids)})
            """,
            missing_dependency_ids,
        ).fetchall()
        tasks_by_id.update(
            {
                dependency.task_id: dependency
                for dependency in (_task_from_row(row) for row in rows)
            }
        )
    for task in tasks:
        refresh_agent_task_dependency_state(task, tasks_by_id)
    return tasks


def missing_dependency_task_ids(dependency_task_ids: list[str], store: AgentTaskStore) -> list[str]:
    return [task_id for task_id in _unique_task_ids(dependency_task_ids) if store.get_agent_task(task_id) is None]


def mark_agent_task_assigned(task: AgentTask, *, work_item_id: str) -> AgentTask:
    append_lifecycle_event(
        task,
        "assigned",
        status=AgentTaskStatus.ASSIGNED,
        metadata={"agent_bus_work_item_id": work_item_id, "target_agent": task.target_agent},
    )
    task.agent_bus_work_item_id = work_item_id
    task.agent_bus_dispatch_error = None
    task.assigned_at = task.updated_at
    return task


def mark_agent_task_review_pending(
    task: AgentTask, *, review_work_item_id: str
) -> AgentTask:
    append_lifecycle_event(
        task,
        "review_gate_pending",
        status=AgentTaskStatus.READY_FOR_REVIEW,
        metadata={"agent_bus_review_work_item_id": review_work_item_id},
    )
    task.completed_at = None
    return task


def mark_agent_task_review_approved(
    task: AgentTask, *, review_packet_id: str | None = None
) -> AgentTask:
    append_lifecycle_event(
        task,
        "review_gate_approved",
        actor="bb2",
        status=AgentTaskStatus.COMPLETED,
        metadata={"bb2_review_packet_id": review_packet_id},
    )
    task.completed_at = task.updated_at
    return task


def mark_agent_task_review_changes_requested(
    task: AgentTask, *, decision: str
) -> AgentTask:
    append_lifecycle_event(
        task,
        "review_gate_changes_requested",
        actor="bb2",
        status=AgentTaskStatus.READY_FOR_REVIEW,
        metadata={"decision": decision},
    )
    return task


def mark_agent_task_dispatch_failed(task: AgentTask, error: str) -> AgentTask:
    append_lifecycle_event(
        task,
        "agent_bus_dispatch_failed",
        status=AgentTaskStatus.FAILED,
        metadata={"error": error},
    )
    task.agent_bus_dispatch_error = error
    task.failed_at = task.updated_at
    return task


def apply_execution_result(task: AgentTask, result: AgentTaskExecutionResult) -> AgentTask:
    now_status = result.status
    append_lifecycle_event(
        task,
        now_status.value,
        actor=result.agent_id,
        status=now_status,
        metadata={
            "commit_sha": result.commit_sha,
            "branch": result.branch,
            "changed_files": result.changed_files,
            "evidence": result.evidence,
        },
    )
    task.branch = result.branch
    task.commit_sha = result.commit_sha
    task.changed_files = result.changed_files
    task.execution_evidence = _execution_evidence_with_preserved_orchestration_metadata(task, result)
    if now_status == AgentTaskStatus.CLAIMED:
        task.claimed_at = task.updated_at
    elif now_status in {AgentTaskStatus.RUNNING, AgentTaskStatus.IN_PROGRESS}:
        task.running_at = task.updated_at
    elif now_status == AgentTaskStatus.COMPLETED:
        task.completed_at = task.updated_at
    elif now_status == AgentTaskStatus.FAILED:
        task.failed_at = task.updated_at
    elif now_status == AgentTaskStatus.CANCELLED:
        task.cancelled_at = task.updated_at
    return task


def append_lifecycle_event(
    task: AgentTask,
    event: str,
    *,
    actor: str = "orchestrator",
    status: AgentTaskStatus | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgentTask:
    now = datetime.now(UTC)
    task.updated_at = now
    if status is not None:
        task.status = status
    task.lifecycle_events.append(
        AgentTaskLifecycleEvent(
            event=event,
            occurred_at=now,
            actor=actor,
            metadata={key: value for key, value in (metadata or {}).items() if value is not None},
        )
    )
    return task


def build_agent_task_store(db_path: str | None) -> AgentTaskStore:
    if not db_path:
        return agent_task_store
    try:
        return SQLiteAgentTaskStore(db_path)
    except (OSError, sqlite3.Error):
        return agent_task_store


def _task_from_row(row: sqlite3.Row) -> AgentTask:
    data = dict(row)
    data["body"] = data.get("body")
    data["labels"] = json.loads(data.get("labels") or "[]")
    data["instructions"] = json.loads(data["instructions"] or "[]")
    data["acceptance_criteria"] = json.loads(data["acceptance_criteria"] or "[]")
    data["dependency_task_ids"] = json.loads(data.get("dependency_task_ids") or "[]")
    data["blocked"] = bool(data.get("blocked") or False)
    data["blocked_by"] = json.loads(data.get("blocked_by") or "[]")
    data["changed_files"] = json.loads(data.get("changed_files") or "[]")
    data["execution_evidence"] = json.loads(data.get("execution_evidence") or "{}")
    data["lifecycle_events"] = json.loads(data["lifecycle_events"] or "[]")
    return AgentTask.model_validate(data)


def _filter_agent_tasks_for_workflow_collection(
    tasks: list[AgentTask],
    *,
    workflow_filter: str,
    recent_since: datetime | None,
) -> list[AgentTask]:
    if workflow_filter == "all":
        return tasks
    if workflow_filter == "active":
        return [task for task in tasks if _agent_task_workflow_active(task)]
    if workflow_filter == "recent":
        return [
            task
            for task in tasks
            if recent_since is not None
            and _agent_task_workflow_last_activity_at(task) >= recent_since
        ]
    return [
        task
        for task in tasks
        if _agent_task_workflow_active(task)
        or (
            recent_since is not None
            and _agent_task_workflow_last_activity_at(task) >= recent_since
        )
    ]


def _sort_agent_tasks_for_workflow_collection(tasks: list[AgentTask]) -> list[AgentTask]:
    return sorted(
        sorted(tasks, key=lambda task: f"wf-agent-task-{task.task_id}"),
        key=lambda task: (
            _agent_task_workflow_last_activity_at(task),
            task.updated_at,
            task.created_at,
        ),
        reverse=True,
    )


def _agent_task_workflow_active(task: AgentTask) -> bool:
    return task.status != AgentTaskStatus.COMPLETED


def _agent_task_workflow_last_activity_at(task: AgentTask) -> datetime:
    values = [
        value
        for value in (
            task.updated_at,
            task.completed_at,
            task.failed_at,
            task.cancelled_at,
            task.running_at,
            task.claimed_at,
            task.assigned_at,
            task.queued_at,
            task.created_at,
        )
        if value is not None
    ]
    return max(values)


def _agent_task_workflow_filter_sql(workflow_filter: str, recent_since: datetime | None) -> tuple[str, tuple[object, ...]]:
    if workflow_filter == "all":
        return "", ()
    if workflow_filter == "active":
        return "WHERE status <> ?", (AgentTaskStatus.COMPLETED.value,)
    if workflow_filter == "recent":
        return f"WHERE {_AGENT_TASK_WORKFLOW_LAST_ACTIVITY_SQL} >= ?", (_dt(recent_since),)
    return (
        f"WHERE status <> ? OR {_AGENT_TASK_WORKFLOW_LAST_ACTIVITY_SQL} >= ?",
        (AgentTaskStatus.COMPLETED.value, _dt(recent_since)),
    )


def _execution_evidence_with_preserved_orchestration_metadata(
    task: AgentTask,
    result: AgentTaskExecutionResult,
) -> dict[str, Any]:
    evidence = dict(result.evidence) if isinstance(result.evidence, dict) else {}
    existing = task.execution_evidence if isinstance(task.execution_evidence, dict) else {}
    for key in ("_workflow_chain", "_routing"):
        if key not in evidence and isinstance(existing.get(key), dict):
            evidence[key] = existing[key]
    return evidence


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if column_name in columns:
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _dt(value: object | None) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _task_objective(request: AgentTaskCreateRequest) -> str:
    return (request.objective or request.body or "").strip()


def _unique_task_ids(task_ids: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for task_id in task_ids:
        normalized = task_id.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


_AGENT_TASK_EXTRA_COLUMNS = [
    ("body", "TEXT"),
    ("labels", "TEXT NOT NULL DEFAULT '[]'"),
    ("dependency_task_ids", "TEXT NOT NULL DEFAULT '[]'"),
    ("blocked", "INTEGER NOT NULL DEFAULT 0"),
    ("blocked_by", "TEXT NOT NULL DEFAULT '[]'"),
    ("agent_bus_work_item_id", "TEXT"),
    ("agent_bus_dispatch_error", "TEXT"),
    ("assigned_at", "TEXT"),
    ("claimed_at", "TEXT"),
    ("running_at", "TEXT"),
    ("completed_at", "TEXT"),
    ("failed_at", "TEXT"),
    ("cancelled_at", "TEXT"),
    ("branch", "TEXT"),
    ("commit_sha", "TEXT"),
    ("changed_files", "TEXT NOT NULL DEFAULT '[]'"),
    ("execution_evidence", "TEXT NOT NULL DEFAULT '{}'"),
    ("lifecycle_event_count", "INTEGER NOT NULL DEFAULT 1"),
    ("last_actor", "TEXT"),
]


_AGENT_TASK_WORKFLOW_LAST_ACTIVITY_SQL = """
max(
    COALESCE(updated_at, ''),
    COALESCE(completed_at, ''),
    COALESCE(failed_at, ''),
    COALESCE(cancelled_at, ''),
    COALESCE(running_at, ''),
    COALESCE(claimed_at, ''),
    COALESCE(assigned_at, ''),
    COALESCE(queued_at, ''),
    COALESCE(created_at, '')
)
"""


def _last_agent_task_lifecycle_actor(task: AgentTask) -> str | None:
    return task.lifecycle_events[-1].actor if task.lifecycle_events else None


def _agent_task_lifecycle_event_count(task: AgentTask) -> int:
    return max(len(task.lifecycle_events), 1)
