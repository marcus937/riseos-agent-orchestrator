import sqlite3
from pathlib import Path

from app.event_store import EventRecord
from app.review_queue import ReviewQueueCounters, ReviewWorkItem, ReviewWorkItemStatus


class SQLiteStateStore:
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
                CREATE TABLE IF NOT EXISTS event_records (
                    event_id TEXT PRIMARY KEY,
                    github_event TEXT NOT NULL,
                    repo_full_name TEXT,
                    branch TEXT,
                    commit_sha TEXT,
                    issue_number INTEGER,
                    pr_number INTEGER,
                    received_at TEXT NOT NULL,
                    raw_action TEXT
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

    def save_event_record(self, record: EventRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO event_records (
                    event_id,
                    github_event,
                    repo_full_name,
                    branch,
                    commit_sha,
                    issue_number,
                    pr_number,
                    received_at,
                    raw_action
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.event_id,
                    str(record.github_event),
                    record.repo_full_name,
                    record.branch,
                    record.commit_sha,
                    record.issue_number,
                    record.pr_number,
                    record.received_at.isoformat(),
                    record.raw_action,
                ),
            )

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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO review_work_items (
                    id,
                    created_at,
                    repo_full_name,
                    event_type,
                    branch,
                    commit_sha,
                    issue_number,
                    pr_number,
                    status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.created_at.isoformat(),
                    item.repo_full_name,
                    str(item.event_type),
                    item.branch,
                    item.commit_sha,
                    item.issue_number,
                    item.pr_number,
                    str(item.status),
                ),
            )

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
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM review_work_items
                GROUP BY status
                """
            ).fetchall()
        status_counts = {str(row["status"]): int(row["count"]) for row in rows}
        approved_count = status_counts.get(ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW.value, 0)
        return ReviewQueueCounters(
            review_queue_count=sum(status_counts.values()),
            pending_review_count=status_counts.get(ReviewWorkItemStatus.PENDING_REVIEW.value, 0),
            reviewing_count=status_counts.get(ReviewWorkItemStatus.REVIEWING.value, 0),
            needs_changes_count=status_counts.get(ReviewWorkItemStatus.NEEDS_CHANGES.value, 0),
            approved_count=approved_count,
            approved_for_human_review_count=approved_count,
            blocked_count=status_counts.get(ReviewWorkItemStatus.BLOCKED.value, 0),
        )

    def _event_record_from_row(self, row: sqlite3.Row) -> EventRecord:
        return EventRecord.model_validate(dict(row))

    def _review_work_item_from_row(self, row: sqlite3.Row) -> ReviewWorkItem:
        return ReviewWorkItem.model_validate(dict(row))


def build_sqlite_store(db_path: str | None) -> SQLiteStateStore | None:
    if not db_path:
        return None
    try:
        return SQLiteStateStore(db_path)
    except OSError:
        return None
    except sqlite3.Error:
        return None
