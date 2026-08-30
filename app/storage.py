import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.event_store import EventRecord
from app.operational_logging import log_event
from app.review_queue import ReviewQueueCounters, ReviewWorkItem, ReviewWorkItemStatus, review_queue_counters, review_work_item_identity
from app.workflow_chain_diagnostics import log_workflow_chain_availability


class SQLiteStateStore:
    def __init__(self, db_path: str, *, max_review_items: int = 500) -> None:
        self.db_path = Path(db_path)
        self.max_review_items = max_review_items
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
                CREATE TABLE IF NOT EXISTS event_records (
                    event_id TEXT PRIMARY KEY,
                    github_event TEXT NOT NULL,
                    diagnostic_stage TEXT NOT NULL DEFAULT 'webhook_accepted',
                    correlation_key TEXT,
                    repo_full_name TEXT,
                    branch TEXT,
                    commit_sha TEXT,
                    issue_number INTEGER,
                    pr_number INTEGER,
                    pr_merged INTEGER,
                    received_at TEXT NOT NULL,
                    raw_action TEXT
                )
                """
            )
            _ensure_column(conn, "event_records", "diagnostic_stage", "TEXT NOT NULL DEFAULT 'webhook_accepted'")
            _ensure_column(conn, "event_records", "correlation_key", "TEXT")
            _ensure_column(conn, "event_records", "pr_merged", "INTEGER")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS issue_dispatch_claims (
                    issue_key TEXT PRIMARY KEY,
                    claimed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_work_items (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    repo_full_name TEXT,
                    event_type TEXT NOT NULL,
                    branch TEXT,
                    commit_sha TEXT,
                    issue_number INTEGER,
                    pr_number INTEGER,
                    status TEXT NOT NULL
                )
                """
            )
            for column_name, column_type in _REVIEW_WORK_ITEM_EXTRA_COLUMNS:
                _ensure_column(conn, "review_work_items", column_name, column_type)

    def has_event_record(self, event_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM event_records WHERE event_id = ?", (event_id,)).fetchone()
        return row is not None

    def save_event_record(self, record: EventRecord) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO event_records (
                    event_id,
                    github_event,
                    diagnostic_stage,
                    correlation_key,
                    repo_full_name,
                    branch,
                    commit_sha,
                    issue_number,
                    pr_number,
                    pr_merged,
                    received_at,
                    raw_action
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.event_id,
                    str(record.github_event),
                    record.diagnostic_stage,
                    record.correlation_key,
                    record.repo_full_name,
                    record.branch,
                    record.commit_sha,
                    record.issue_number,
                    record.pr_number,
                    _bool(record.pr_merged),
                    record.received_at.isoformat(),
                    record.raw_action,
                ),
            )
        return cursor.rowcount == 1

    def already_dispatched(self, issue_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM issue_dispatch_claims WHERE issue_key = ?", (issue_key,)).fetchone()
        return row is not None

    def claim_issue_dispatch(self, issue_key: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO issue_dispatch_claims (issue_key, claimed_at)
                VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (issue_key,),
            )
        return cursor.rowcount == 1

    def mark_dispatched(self, issue_key: str) -> None:
        self.claim_issue_dispatch(issue_key)

    def recent_events(self, limit: int = 50) -> list[EventRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM event_records
                ORDER BY received_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._event_record_from_row(row) for row in rows]

    def event_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM event_records").fetchone()
        return int(row["count"])

    def save_review_work_item(self, item: ReviewWorkItem) -> None:
        log_workflow_chain_availability(
            "wf_chain_metadata_storage_save_review_work_item_input",
            item,
        )
        runtime_validation_context_json = _json(item.runtime_validation_context)
        _log_review_work_item_persistence_json(
            "wf_chain_metadata_storage_before_save_json",
            item_id=item.id,
            runtime_validation_context=item.runtime_validation_context,
            raw_json_stored=runtime_validation_context_json,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO review_work_items (
                    id,
                    created_at,
                    updated_at,
                    repo_full_name,
                    event_type,
                    branch,
                    base_branch,
                    commit_sha,
                    issue_number,
                    pr_number,
                    status,
                    lifecycle_stage,
                    worker_claimed_at,
                    review_started_at,
                    openai_review_attempted_at,
                    openai_review_completed_at,
                    review_completed_at,
                    github_writeback_started_at,
                    github_writeback_completed_at,
                    github_writeback_success,
                    agent_bus_dispatch_started_at,
                    agent_bus_dispatch_completed_at,
                    agent_bus_dispatch_success,
                    agent_bus_work_item_id,
                    agent_bus_dispatch_error,
                    runtime_validation_id,
                    runtime_validation_status,
                    runtime_validation_digest,
                    runtime_validation_completed_at,
                    runtime_validation_context,
                    failure_count,
                    last_failure_at,
                    last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.created_at.isoformat(),
                    _dt(item.updated_at),
                    item.repo_full_name,
                    str(item.event_type),
                    item.branch,
                    item.base_branch,
                    item.commit_sha,
                    item.issue_number,
                    item.pr_number,
                    str(item.status),
                    str(item.lifecycle_stage),
                    _dt(item.worker_claimed_at),
                    _dt(item.review_started_at),
                    _dt(item.openai_review_attempted_at),
                    _dt(item.openai_review_completed_at),
                    _dt(item.review_completed_at),
                    _dt(item.github_writeback_started_at),
                    _dt(item.github_writeback_completed_at),
                    _bool(item.github_writeback_success),
                    _dt(item.agent_bus_dispatch_started_at),
                    _dt(item.agent_bus_dispatch_completed_at),
                    _bool(item.agent_bus_dispatch_success),
                    item.agent_bus_work_item_id,
                    item.agent_bus_dispatch_error,
                    item.runtime_validation_id,
                    item.runtime_validation_status,
                    item.runtime_validation_digest,
                    _dt(item.runtime_validation_completed_at),
                    runtime_validation_context_json,
                    item.failure_count,
                    _dt(item.last_failure_at),
                    item.last_error,
                ),
            )
            raw_row = conn.execute(
                "SELECT runtime_validation_context FROM review_work_items WHERE id = ?",
                (item.id,),
            ).fetchone()
        _log_review_work_item_persistence_json(
            "wf_chain_metadata_storage_after_save_json",
            item_id=item.id,
            raw_json_stored=raw_row["runtime_validation_context"] if raw_row is not None else None,
        )
        self.prune_processed_review_items(self.max_review_items)

    def find_pending_duplicate(self, item: ReviewWorkItem) -> ReviewWorkItem | None:
        repo_full_name, event_type, commit_sha, pr_number, issue_number = review_work_item_identity(item)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM review_work_items
                WHERE status IN (?, ?, ?)
                  AND (repo_full_name IS ? OR repo_full_name = ?)
                  AND event_type = ?
                  AND (commit_sha IS ? OR commit_sha = ?)
                  AND (pr_number IS ? OR pr_number = ?)
                  AND (issue_number IS ? OR issue_number = ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (
                    ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING.value,
                    ReviewWorkItemStatus.PENDING_REVIEW.value,
                    ReviewWorkItemStatus.REVIEWING.value,
                    repo_full_name,
                    repo_full_name,
                    event_type,
                    commit_sha,
                    commit_sha,
                    pr_number,
                    pr_number,
                    issue_number,
                    issue_number,
                ),
            ).fetchone()
        if row is None:
            return None
        return self._review_work_item_from_row(row)

    def claim_review_work_item(self, item_id: str) -> ReviewWorkItem | None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE review_work_items
                SET status = ?,
                    lifecycle_stage = 'worker_claimed',
                    worker_claimed_at = COALESCE(worker_claimed_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND status = ?
                """,
                (ReviewWorkItemStatus.REVIEWING.value, item_id, ReviewWorkItemStatus.PENDING_REVIEW.value),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM review_work_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        return self._review_work_item_from_row(row)

    def reset_review_work_item_for_retry(self, item_id: str, *, error: str | None = None) -> ReviewWorkItem | None:
        with self._connect() as conn:
            if error:
                conn.execute(
                    """
                    UPDATE review_work_items
                    SET status = ?,
                        lifecycle_stage = 'review_failed',
                        failure_count = failure_count + 1,
                        last_failure_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        last_error = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ? AND status = ?
                    """,
                    (ReviewWorkItemStatus.PENDING_REVIEW.value, error, item_id, ReviewWorkItemStatus.REVIEWING.value),
                )
            else:
                conn.execute(
                    """
                    UPDATE review_work_items
                    SET status = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ? AND status = ?
                    """,
                    (ReviewWorkItemStatus.PENDING_REVIEW.value, item_id, ReviewWorkItemStatus.REVIEWING.value),
                )
            row = conn.execute("SELECT * FROM review_work_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        return self._review_work_item_from_row(row)

    def reclaim_stale_review_claims(self, *, older_than_seconds: int) -> list[ReviewWorkItem]:
        if older_than_seconds <= 0:
            return []

        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM review_work_items
                WHERE status = ?
                  AND worker_claimed_at IS NOT NULL
                  AND worker_claimed_at < ?
                ORDER BY worker_claimed_at ASC
                """,
                (ReviewWorkItemStatus.REVIEWING.value, cutoff.isoformat()),
            ).fetchall()
            item_ids = [str(row["id"]) for row in rows]
            if not item_ids:
                return []

            conn.executemany(
                """
                UPDATE review_work_items
                SET status = ?,
                    lifecycle_stage = 'review_failed',
                    failure_count = failure_count + 1,
                    last_failure_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    last_error = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND status = ?
                """,
                [
                    (
                        ReviewWorkItemStatus.PENDING_REVIEW.value,
                        "Recovered stale worker claim after restart.",
                        item_id,
                        ReviewWorkItemStatus.REVIEWING.value,
                    )
                    for item_id in item_ids
                ],
            )
            reloaded = conn.execute(
                f"SELECT * FROM review_work_items WHERE id IN ({','.join('?' for _ in item_ids)})",
                item_ids,
            ).fetchall()
        return [self._review_work_item_from_row(row) for row in reloaded]

    def list_review_work_items(self) -> list[ReviewWorkItem]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM review_work_items
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [self._review_work_item_from_row(row) for row in rows]

    def get_review_work_item(self, item_id: str) -> ReviewWorkItem | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM review_work_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        return self._review_work_item_from_row(row)

    def review_queue_counters(self) -> ReviewQueueCounters:
        return review_queue_counters(self.list_review_work_items())

    def prune_processed_review_items(self, max_items: int | None = None) -> int:
        limit = max_items or self.max_review_items
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM review_work_items").fetchone()
            overage = int(row["count"]) - limit
            if overage <= 0:
                return 0
            rows = conn.execute(
                """
                SELECT id FROM review_work_items
                WHERE status NOT IN (?, ?, ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (
                    ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING.value,
                    ReviewWorkItemStatus.PENDING_REVIEW.value,
                    ReviewWorkItemStatus.REVIEWING.value,
                    overage,
                ),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if not ids:
                return 0
            conn.executemany("DELETE FROM review_work_items WHERE id = ?", [(item_id,) for item_id in ids])
        return len(ids)

    def _event_record_from_row(self, row: sqlite3.Row) -> EventRecord:
        data = dict(row)
        if data.get("pr_merged") is not None:
            data["pr_merged"] = bool(data["pr_merged"])
        return EventRecord.model_validate(data)

    def _review_work_item_from_row(self, row: sqlite3.Row) -> ReviewWorkItem:
        data = dict(row)
        raw_runtime_validation_context = data.get("runtime_validation_context")
        _log_review_work_item_persistence_json(
            "wf_chain_metadata_storage_before_deserialize_json",
            item_id=data.get("id"),
            raw_json_loaded=raw_runtime_validation_context,
        )
        if data.get("github_writeback_success") is not None:
            data["github_writeback_success"] = bool(data["github_writeback_success"])
        if data.get("agent_bus_dispatch_success") is not None:
            data["agent_bus_dispatch_success"] = bool(data["agent_bus_dispatch_success"])
        loaded_runtime_validation_context = _load_json(raw_runtime_validation_context)
        _log_review_work_item_persistence_json(
            "wf_chain_metadata_storage_after_load_json",
            item_id=data.get("id"),
            runtime_validation_context=loaded_runtime_validation_context,
            raw_json_loaded=raw_runtime_validation_context,
        )
        data["runtime_validation_context"] = loaded_runtime_validation_context
        log_workflow_chain_availability(
            "wf_chain_metadata_storage_deserialize_review_work_item_row",
            data,
        )
        item = ReviewWorkItem.model_validate(data)
        _log_review_work_item_persistence_json(
            "wf_chain_metadata_storage_after_model_validate",
            item_id=item.id,
            runtime_validation_context=item.runtime_validation_context,
        )
        log_workflow_chain_availability(
            "wf_chain_metadata_storage_deserialize_review_work_item_model",
            item,
        )
        return item


def build_sqlite_store(db_path: str | None, *, max_review_items: int = 500) -> SQLiteStateStore | None:
    if not db_path:
        return None
    try:
        return SQLiteStateStore(db_path, max_review_items=max_review_items)
    except OSError:
        return None
    except sqlite3.Error:
        return None


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


def _bool(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _json(value: object | None) -> str:
    return json.dumps(value or {}, sort_keys=True, default=str)


def _load_json(value: object | None) -> dict[str, object]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _log_review_work_item_persistence_json(
    event: str,
    *,
    item_id: object | None,
    runtime_validation_context: object | None = None,
    raw_json_stored: object | None = None,
    raw_json_loaded: object | None = None,
) -> None:
    context = runtime_validation_context if isinstance(runtime_validation_context, dict) else None
    if context is None:
        raw_json = raw_json_stored if raw_json_stored is not None else raw_json_loaded
        context = _load_json(raw_json)
    review_dispatch = context.get("review_dispatch") if isinstance(context.get("review_dispatch"), dict) else {}
    metadata = context.get("metadata") if isinstance(context.get("metadata"), dict) else {}
    if not metadata and isinstance(review_dispatch.get("metadata"), dict):
        metadata = review_dispatch["metadata"]
    workflow_chain = _first_dict(
        context.get("workflow_chain"),
        context.get("_workflow_chain"),
        context.get("workflowChain"),
        review_dispatch.get("workflow_chain"),
        review_dispatch.get("_workflow_chain"),
        review_dispatch.get("workflowChain"),
        metadata.get("workflow_chain") if isinstance(metadata, dict) else None,
        metadata.get("_workflow_chain") if isinstance(metadata, dict) else None,
    )
    log_event(
        event,
        item_id=item_id,
        metadata_keys=sorted(str(key) for key in metadata.keys()) if isinstance(metadata, dict) else [],
        runtime_context_keys=sorted(str(key) for key in context.keys()),
        review_dispatch_keys=sorted(str(key) for key in review_dispatch.keys()),
        workflow_chain_keys=sorted(str(key) for key in workflow_chain.keys()),
        workflow_chain_populated=bool(workflow_chain),
        runtime_context_populated=bool(context),
        review_dispatch_populated=bool(review_dispatch),
        raw_json_stored=raw_json_stored,
        raw_json_loaded=raw_json_loaded,
        _include_nulls=True,
    )


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict) and value:
            return value
    return {}


_REVIEW_WORK_ITEM_EXTRA_COLUMNS = [
    ("base_branch", "TEXT"),
    ("updated_at", "TEXT"),
    ("lifecycle_stage", "TEXT NOT NULL DEFAULT 'review_queued'"),
    ("worker_claimed_at", "TEXT"),
    ("review_started_at", "TEXT"),
    ("openai_review_attempted_at", "TEXT"),
    ("openai_review_completed_at", "TEXT"),
    ("review_completed_at", "TEXT"),
    ("github_writeback_started_at", "TEXT"),
    ("github_writeback_completed_at", "TEXT"),
    ("github_writeback_success", "INTEGER"),
    ("agent_bus_dispatch_started_at", "TEXT"),
    ("agent_bus_dispatch_completed_at", "TEXT"),
    ("agent_bus_dispatch_success", "INTEGER"),
    ("agent_bus_work_item_id", "TEXT"),
    ("agent_bus_dispatch_error", "TEXT"),
    ("runtime_validation_id", "TEXT"),
    ("runtime_validation_status", "TEXT"),
    ("runtime_validation_digest", "TEXT"),
    ("runtime_validation_completed_at", "TEXT"),
    ("runtime_validation_context", "TEXT"),
    ("failure_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_failure_at", "TEXT"),
    ("last_error", "TEXT"),
]
