from __future__ import annotations

import json
import sqlite3
from collections import deque
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, Field


class AgentTaskStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    READY_FOR_REVIEW = "ready_for_review"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentTaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class AgentTaskLifecycleEvent(BaseModel):
    event: str
    occurred_at: datetime
    actor: str = "orchestrator"
    metadata: dict[str, object] = Field(default_factory=dict)


class AgentTaskCreateRequest(BaseModel):
    repo_full_name: str = Field(min_length=1)
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    instructions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    target_agent: str = Field(min_length=1)
    priority: AgentTaskPriority = AgentTaskPriority.NORMAL
    correlation_id: str | None = None


class AgentTaskCreateResponse(BaseModel):
    task_id: str
    status: AgentTaskStatus
    created_at: datetime
    target_agent: str


class AgentTask(BaseModel):
    task_id: str
    repo_full_name: str
    title: str
    objective: str
    instructions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    target_agent: str
    priority: AgentTaskPriority = AgentTaskPriority.NORMAL
    correlation_id: str | None = None
    status: AgentTaskStatus = AgentTaskStatus.CREATED
    source: str = "direct_api"
    issue_number: int | None = None
    created_at: datetime
    updated_at: datetime
    queued_at: datetime | None = None
    lifecycle_events: list[AgentTaskLifecycleEvent] = Field(default_factory=list)


class AgentTaskStore(Protocol):
    def save_agent_task(self, task: AgentTask) -> None:
        ...

    def list_agent_tasks(self) -> list[AgentTask]:
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    queued_at TEXT,
                    lifecycle_events TEXT NOT NULL
                )
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
                    instructions,
                    acceptance_criteria,
                    target_agent,
                    priority,
                    correlation_id,
                    status,
                    source,
                    issue_number,
                    created_at,
                    updated_at,
                    queued_at,
                    lifecycle_events
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.repo_full_name,
                    task.title,
                    task.objective,
                    json.dumps(task.instructions),
                    json.dumps(task.acceptance_criteria),
                    task.target_agent,
                    task.priority.value,
                    task.correlation_id,
                    task.status.value,
                    task.source,
                    task.issue_number,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                    task.queued_at.isoformat() if task.queued_at else None,
                    json.dumps([event.model_dump(mode="json") for event in task.lifecycle_events]),
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
        return [_task_from_row(row) for row in rows]

    def get_agent_task(self, task_id: str) -> AgentTask | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)).fetchone()
        return _task_from_row(row) if row is not None else None


agent_task_store = InMemoryAgentTaskStore()


def create_agent_task(request: AgentTaskCreateRequest) -> AgentTask:
    now = datetime.now(UTC)
    task_id = f"agtask-{uuid4()}"
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
        objective=request.objective,
        instructions=request.instructions,
        acceptance_criteria=request.acceptance_criteria,
        target_agent=request.target_agent,
        priority=request.priority,
        correlation_id=request.correlation_id,
        status=AgentTaskStatus.QUEUED,
        source="direct_api",
        created_at=now,
        updated_at=now,
        queued_at=now,
        lifecycle_events=[created, queued],
    )


def build_agent_task_store(db_path: str | None) -> AgentTaskStore:
    if not db_path:
        return agent_task_store
    try:
        return SQLiteAgentTaskStore(db_path)
    except (OSError, sqlite3.Error):
        return agent_task_store


def _task_from_row(row: sqlite3.Row) -> AgentTask:
    data = dict(row)
    data["instructions"] = json.loads(data["instructions"] or "[]")
    data["acceptance_criteria"] = json.loads(data["acceptance_criteria"] or "[]")
    data["lifecycle_events"] = json.loads(data["lifecycle_events"] or "[]")
    return AgentTask.model_validate(data)
