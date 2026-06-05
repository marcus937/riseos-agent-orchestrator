from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


ACTIVE_CLAIM_STATUSES = {"active"}
COMPLETED_CLAIM_STATUS = "completed"
DUPLICATE_DECISION = "already_claimed"
CLAIMED_DECISION = "claimed"
REQUEUED_DECISION = "requeued"


@dataclass(frozen=True)
class TaskClaimRequest:
    repo_full_name: str
    issue_number: int | None
    pr_number: int | None
    task_type: str
    branch_rule: str
    source: str
    allow_completed_requeue: bool = False


@dataclass(frozen=True)
class TaskClaimResult:
    decision: str
    claim: dict[str, Any]
    duplicate_of_claim_id: str | None = None


class TaskClaimRegistry:
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
                CREATE TABLE IF NOT EXISTS task_claims (
                    claim_id TEXT PRIMARY KEY,
                    repo_full_name TEXT NOT NULL,
                    issue_number INTEGER,
                    pr_number INTEGER,
                    task_type TEXT NOT NULL,
                    normalized_branch_rule TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE (
                        repo_full_name,
                        issue_number,
                        pr_number,
                        task_type,
                        normalized_branch_rule,
                        status
                    )
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS duplicate_dispatches (
                    duplicate_id TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL,
                    repo_full_name TEXT NOT NULL,
                    issue_number INTEGER,
                    pr_number INTEGER,
                    task_type TEXT NOT NULL,
                    normalized_branch_rule TEXT NOT NULL,
                    source TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def claim_task(self, request: TaskClaimRequest) -> TaskClaimResult:
        normalized = normalized_claim_request(request)
        existing = self.find_active_claim(normalized)
        if existing is not None:
            duplicate = self.record_duplicate_dispatch(normalized, existing["claim_id"])
            return TaskClaimResult(DUPLICATE_DECISION, duplicate, duplicate_of_claim_id=existing["claim_id"])

        completed = self.find_completed_claim(normalized)
        if completed is not None and not request.allow_completed_requeue:
            duplicate = self.record_duplicate_dispatch(normalized, completed["claim_id"])
            return TaskClaimResult(DUPLICATE_DECISION, duplicate, duplicate_of_claim_id=completed["claim_id"])

        claim = self.create_active_claim(normalized)
        return TaskClaimResult(REQUEUED_DECISION if completed is not None else CLAIMED_DECISION, claim)

    def create_active_claim(self, request: TaskClaimRequest) -> dict[str, Any]:
        now = _now()
        claim_id = str(uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_claims (
                    claim_id,
                    repo_full_name,
                    issue_number,
                    pr_number,
                    task_type,
                    normalized_branch_rule,
                    status,
                    source,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    request.repo_full_name,
                    request.issue_number,
                    request.pr_number,
                    request.task_type,
                    request.branch_rule,
                    "active",
                    request.source,
                    now,
                    now,
                ),
            )
        return self.get_claim(claim_id)

    def complete_claim(self, claim_id: str) -> dict[str, Any] | None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE task_claims
                SET status = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE claim_id = ? AND status = ?
                """,
                (COMPLETED_CLAIM_STATUS, now, now, claim_id, "active"),
            )
        return self.get_claim(claim_id)

    def find_active_claim(self, request: TaskClaimRequest) -> dict[str, Any] | None:
        return self._find_claim(request, statuses=ACTIVE_CLAIM_STATUSES)

    def find_completed_claim(self, request: TaskClaimRequest) -> dict[str, Any] | None:
        return self._find_claim(request, statuses={COMPLETED_CLAIM_STATUS})

    def record_duplicate_dispatch(self, request: TaskClaimRequest, claim_id: str) -> dict[str, Any]:
        now = _now()
        duplicate_id = str(uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO duplicate_dispatches (
                    duplicate_id,
                    claim_id,
                    repo_full_name,
                    issue_number,
                    pr_number,
                    task_type,
                    normalized_branch_rule,
                    source,
                    decision,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    duplicate_id,
                    claim_id,
                    request.repo_full_name,
                    request.issue_number,
                    request.pr_number,
                    request.task_type,
                    request.branch_rule,
                    request.source,
                    DUPLICATE_DECISION,
                    now,
                ),
            )
        return self.get_duplicate_dispatch(duplicate_id)

    def list_claims(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM task_claims ORDER BY created_at ASC, claim_id ASC").fetchall()
        return [dict(row) for row in rows]

    def list_duplicate_dispatches(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM duplicate_dispatches ORDER BY created_at ASC, duplicate_id ASC").fetchall()
        return [dict(row) for row in rows]

    def get_claim(self, claim_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_claims WHERE claim_id = ?", (claim_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task claim not found: {claim_id}")
        return dict(row)

    def get_duplicate_dispatch(self, duplicate_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM duplicate_dispatches WHERE duplicate_id = ?", (duplicate_id,)).fetchone()
        if row is None:
            raise KeyError(f"Duplicate dispatch not found: {duplicate_id}")
        return dict(row)

    def _find_claim(self, request: TaskClaimRequest, *, statuses: set[str]) -> dict[str, Any] | None:
        placeholders = ", ".join("?" for _ in statuses)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM task_claims
                WHERE repo_full_name = ?
                  AND (issue_number IS ? OR issue_number = ?)
                  AND (pr_number IS ? OR pr_number = ?)
                  AND task_type = ?
                  AND normalized_branch_rule = ?
                  AND status IN ({placeholders})
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (
                    request.repo_full_name,
                    request.issue_number,
                    request.issue_number,
                    request.pr_number,
                    request.pr_number,
                    request.task_type,
                    request.branch_rule,
                    *sorted(statuses),
                ),
            ).fetchone()
        return dict(row) if row is not None else None


def normalized_claim_request(request: TaskClaimRequest) -> TaskClaimRequest:
    return TaskClaimRequest(
        repo_full_name=request.repo_full_name.strip().lower(),
        issue_number=request.issue_number,
        pr_number=request.pr_number,
        task_type=_normalize_token(request.task_type),
        branch_rule=normalize_branch_rule(request.branch_rule),
        source=_normalize_token(request.source),
        allow_completed_requeue=request.allow_completed_requeue,
    )


def normalize_branch_rule(branch_rule: str) -> str:
    value = " ".join(branch_rule.strip().lower().replace("`", "").split())
    if value.endswith(" only"):
        value = value.removesuffix(" only").strip()
    if value.startswith("branch:"):
        value = value.removeprefix("branch:").strip()
    return value or "unspecified"


def _normalize_token(value: str) -> str:
    return "_".join(value.strip().lower().replace("-", "_").split())


def _now() -> str:
    return datetime.now(UTC).isoformat()
