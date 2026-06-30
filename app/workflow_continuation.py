from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel

from app.operational_logging import log_event


class WorkflowContinuationStatus(StrEnum):
    PENDING = "PENDING"
    DISPATCHING = "DISPATCHING"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    WAITING_FOR_REVIEW = "WAITING_FOR_REVIEW"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRY_PENDING = "RETRY_PENDING"


class WorkflowContinuation(BaseModel):
    continuation_id: str
    workflow_chain_id: str
    current_workflow_step: str
    next_workflow_step: str
    workflow_steps: list[str] | None = None
    repository: str
    pr_number: int
    branch: str
    base_branch: str | None = None
    previous_work_item_id: str | None = None
    current_work_item_id: str | None = None
    next_work_item_id: str | None = None
    idempotency_key: str
    status: WorkflowContinuationStatus
    dispatch_attempts: int = 0
    last_dispatch_attempt_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class WorkflowContinuationStore(Protocol):
    def resolve_or_create_workflow_continuation(self, payload: dict[str, Any]) -> tuple[WorkflowContinuation, bool]: ...

    def create_failed_workflow_continuation(self, payload: dict[str, Any], *, reason: str) -> WorkflowContinuation: ...

    def acquire_workflow_continuation_lock(
        self,
        continuation_id: str,
        *,
        include_dispatching: bool = False,
    ) -> tuple[WorkflowContinuation, bool]: ...

    def mark_workflow_continuation_dispatched(
        self,
        continuation_id: str,
        *,
        work_item_id: str,
    ) -> WorkflowContinuation: ...

    def mark_workflow_continuation_retry_pending(
        self,
        continuation_id: str,
        *,
        error: str,
    ) -> WorkflowContinuation: ...

    def mark_workflow_continuation_changes_requested(self, continuation_id: str) -> WorkflowContinuation: ...

    def list_retryable_workflow_continuations(self) -> list[WorkflowContinuation]: ...

    def list_workflow_continuations(self, workflow_chain_id: str) -> list[WorkflowContinuation]: ...

    def get_workflow_continuation(self, continuation_id: str) -> WorkflowContinuation | None: ...


class SQLiteWorkflowContinuationStore:
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
                CREATE TABLE IF NOT EXISTS workflow_continuations (
                    continuation_id TEXT PRIMARY KEY,
                    workflow_chain_id TEXT NOT NULL,
                    current_workflow_step TEXT NOT NULL,
                    next_workflow_step TEXT NOT NULL,
                    workflow_steps TEXT,
                    repository TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    branch TEXT NOT NULL,
                    base_branch TEXT,
                    previous_work_item_id TEXT,
                    current_work_item_id TEXT,
                    next_work_item_id TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    dispatch_attempts INTEGER NOT NULL DEFAULT 0,
                    last_dispatch_attempt_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_continuations)").fetchall()}
            if "next_work_item_id" not in columns:
                conn.execute("ALTER TABLE workflow_continuations ADD COLUMN next_work_item_id TEXT")
            if "workflow_steps" not in columns:
                conn.execute("ALTER TABLE workflow_continuations ADD COLUMN workflow_steps TEXT")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_continuations_idempotency
                ON workflow_continuations(idempotency_key)
                """
            )

    def resolve_or_create_workflow_continuation(self, payload: dict[str, Any]) -> tuple[WorkflowContinuation, bool]:
        now = datetime.now(UTC)
        continuation_id = str(payload.get("continuation_id") or uuid4())
        status = str(payload.get("status") or WorkflowContinuationStatus.PENDING.value)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO workflow_continuations (
                    continuation_id,
                    workflow_chain_id,
                    current_workflow_step,
                    next_workflow_step,
                    workflow_steps,
                    repository,
                    pr_number,
                    branch,
                    base_branch,
                    previous_work_item_id,
                    current_work_item_id,
                    next_work_item_id,
                    idempotency_key,
                    status,
                    dispatch_attempts,
                    last_dispatch_attempt_at,
                    last_error,
                    created_at,
                    updated_at,
                    completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, NULL)
                """,
                (
                    continuation_id,
                    str(payload["workflow_chain_id"]),
                    str(payload["current_workflow_step"]),
                    str(payload["next_workflow_step"]),
                    _workflow_steps_json(payload.get("workflow_steps")),
                    str(payload["repository"]),
                    int(payload["pr_number"]),
                    str(payload["branch"]),
                    _optional_text(payload.get("base_branch")),
                    _optional_text(payload.get("previous_work_item_id")),
                    _optional_text(payload.get("current_work_item_id")),
                    _optional_text(payload.get("next_work_item_id")),
                    str(payload["idempotency_key"]),
                    status,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            created = cursor.rowcount == 1
            row = conn.execute(
                "SELECT * FROM workflow_continuations WHERE idempotency_key = ?",
                (str(payload["idempotency_key"]),),
            ).fetchone()
        continuation = _continuation_from_row(row)
        log_continuation_event("CONTINUATION_CREATED" if created else "CONTINUATION_REUSED", continuation)
        return continuation, created

    def create_failed_workflow_continuation(self, payload: dict[str, Any], *, reason: str) -> WorkflowContinuation:
        payload = {**payload, "status": WorkflowContinuationStatus.FAILED.value}
        continuation, _created = self.resolve_or_create_workflow_continuation(payload)
        continuation = self._update_status(
            continuation.continuation_id,
            WorkflowContinuationStatus.FAILED,
            last_error=reason,
            completed_at=datetime.now(UTC),
        )
        log_continuation_event("CONTINUATION_FAILED", continuation, reason=reason)
        return continuation

    def acquire_workflow_continuation_lock(
        self,
        continuation_id: str,
        *,
        include_dispatching: bool = False,
    ) -> tuple[WorkflowContinuation, bool]:
        allowed = [WorkflowContinuationStatus.PENDING.value, WorkflowContinuationStatus.RETRY_PENDING.value]
        if include_dispatching:
            allowed.append(WorkflowContinuationStatus.DISPATCHING.value)
        now = datetime.now(UTC)
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in allowed)
            cursor = conn.execute(
                f"""
                UPDATE workflow_continuations
                SET status = ?,
                    dispatch_attempts = dispatch_attempts + 1,
                    last_dispatch_attempt_at = ?,
                    updated_at = ?
                WHERE continuation_id = ? AND status IN ({placeholders})
                """,
                (
                    WorkflowContinuationStatus.DISPATCHING.value,
                    now.isoformat(),
                    now.isoformat(),
                    continuation_id,
                    *allowed,
                ),
            )
            row = conn.execute("SELECT * FROM workflow_continuations WHERE continuation_id = ?", (continuation_id,)).fetchone()
        continuation = _continuation_from_row(row)
        acquired = cursor.rowcount == 1
        if acquired:
            log_continuation_event("CONTINUATION_LOCK_ACQUIRED", continuation)
            log_continuation_event("CONTINUATION_DISPATCH_STARTED", continuation)
        else:
            log_continuation_event("CONTINUATION_REUSED", continuation)
        return continuation, acquired

    def mark_workflow_continuation_dispatched(
        self,
        continuation_id: str,
        *,
        work_item_id: str,
    ) -> WorkflowContinuation:
        continuation = self._update_status(
            continuation_id,
            WorkflowContinuationStatus.DISPATCHED,
            current_work_item_id=work_item_id,
            next_work_item_id=work_item_id,
            last_error=None,
        )
        log_continuation_event("CONTINUATION_DISPATCHED", continuation)
        return continuation

    def mark_workflow_continuation_retry_pending(
        self,
        continuation_id: str,
        *,
        error: str,
    ) -> WorkflowContinuation:
        continuation = self._update_status(continuation_id, WorkflowContinuationStatus.RETRY_PENDING, last_error=error)
        log_continuation_event("CONTINUATION_RETRY_PENDING", continuation, error=error)
        return continuation

    def mark_workflow_continuation_changes_requested(self, continuation_id: str) -> WorkflowContinuation:
        return self._update_status(continuation_id, WorkflowContinuationStatus.CHANGES_REQUESTED)

    def list_retryable_workflow_continuations(self) -> list[WorkflowContinuation]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM workflow_continuations
                WHERE status IN (?, ?)
                ORDER BY updated_at ASC
                """,
                (WorkflowContinuationStatus.RETRY_PENDING.value, WorkflowContinuationStatus.DISPATCHING.value),
            ).fetchall()
        return [_continuation_from_row(row) for row in rows]

    def list_workflow_continuations(self, workflow_chain_id: str) -> list[WorkflowContinuation]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM workflow_continuations
                WHERE workflow_chain_id = ?
                ORDER BY created_at ASC
                """,
                (workflow_chain_id,),
            ).fetchall()
        return [_continuation_from_row(row) for row in rows]

    def get_workflow_continuation(self, continuation_id: str) -> WorkflowContinuation | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM workflow_continuations WHERE continuation_id = ?", (continuation_id,)).fetchone()
        return _continuation_from_row(row) if row is not None else None

    def _update_status(
        self,
        continuation_id: str,
        status: WorkflowContinuationStatus,
        *,
        current_work_item_id: str | None = None,
        next_work_item_id: str | None = None,
        last_error: str | None = None,
        completed_at: datetime | None = None,
    ) -> WorkflowContinuation:
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE workflow_continuations
                SET status = ?,
                    current_work_item_id = COALESCE(?, current_work_item_id),
                    next_work_item_id = COALESCE(?, next_work_item_id),
                    last_error = ?,
                    completed_at = COALESCE(?, completed_at),
                    updated_at = ?
                WHERE continuation_id = ?
                """,
                (
                    status.value,
                    current_work_item_id,
                    next_work_item_id,
                    last_error,
                    completed_at.isoformat() if completed_at else None,
                    now.isoformat(),
                    continuation_id,
                ),
            )
            row = conn.execute("SELECT * FROM workflow_continuations WHERE continuation_id = ?", (continuation_id,)).fetchone()
        return _continuation_from_row(row)


def build_workflow_continuation_store(db_path: str | None) -> SQLiteWorkflowContinuationStore | None:
    if not db_path:
        return None
    try:
        return SQLiteWorkflowContinuationStore(db_path)
    except (OSError, sqlite3.Error):
        return None


def workflow_continuation_idempotency_key(
    *,
    workflow_chain_id: str,
    repository: str | None = None,
    pr_number: int,
    branch: str,
    next_workflow_step: str,
) -> str:
    if repository:
        return f"workflow-chain:{workflow_chain_id}:{repository}:{pr_number}:{branch}:{next_workflow_step}"
    return f"wf-chain:{workflow_chain_id}:{pr_number}:{branch}:{next_workflow_step}"


def log_continuation_event(event_name: str, continuation: WorkflowContinuation, **extra: Any) -> None:
    work_item_id = continuation.next_work_item_id or continuation.current_work_item_id
    log_event(
        event_name,
        workflow_chain_id=continuation.workflow_chain_id,
        current_workflow_step=continuation.current_workflow_step,
        next_workflow_step=continuation.next_workflow_step,
        continuation_id=continuation.continuation_id,
        idempotency_key=continuation.idempotency_key,
        work_item_id=work_item_id,
        pr_number=continuation.pr_number,
        branch=continuation.branch,
        status=continuation.status.value,
        retry_count=continuation.dispatch_attempts,
        **extra,
    )


def _continuation_from_row(row: sqlite3.Row | None) -> WorkflowContinuation:
    if row is None:
        raise LookupError("Workflow continuation row not found.")
    data = dict(row)
    if not data.get("next_work_item_id"):
        data["next_work_item_id"] = data.get("current_work_item_id")
    data["workflow_steps"] = _workflow_steps_from_json(data.get("workflow_steps"))
    return WorkflowContinuation.model_validate(data)


def _workflow_steps_json(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return json.dumps([str(item) for item in value if str(item).strip()])
    return None


def _workflow_steps_from_json(value: Any) -> list[str] | None:
    if not value:
        return None
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [part.strip() for part in value.split(",")]
        if isinstance(parsed, list):
            steps = [str(item).strip() for item in parsed if str(item).strip()]
            return steps or None
    return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
