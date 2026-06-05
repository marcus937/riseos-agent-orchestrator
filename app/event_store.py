from collections import deque
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from pydantic import BaseModel

from app.correlation import branch_from_parsed, correlation_id_from_key, correlation_key_from_parsed
from app.github_events import GitHubEventType, ParsedGitHubEvent
from app.review_queue import ReviewQueueCounters


class EventRecord(BaseModel):
    event_id: str
    github_event: GitHubEventType
    diagnostic_stage: str = "webhook_accepted"
    correlation_id: str | None = None
    correlation_key: str | None = None
    repo_full_name: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    received_at: datetime
    raw_action: str | None = None


class DebugHealth(BaseModel):
    webhook_count: int
    accepted_count: int
    rejected_count: int
    uptime: float
    review_queue_count: int
    pending_review_count: int
    reviewing_count: int
    needs_changes_count: int
    approved_count: int
    approved_for_human_review_count: int
    blocked_count: int


class InMemoryEventStore:
    def __init__(self, max_records: int = 50) -> None:
        self._records: deque[EventRecord] = deque(maxlen=max_records)
        self._event_ids: set[str] = set()
        self._started_at = monotonic()
        self.webhook_count = 0
        self.accepted_count = 0
        self.rejected_count = 0
        self.duplicate_count = 0

    def has_event_id(self, event_id: str) -> bool:
        return event_id in self._event_ids

    def record_accepted(self, parsed: ParsedGitHubEvent, *, event_id: str | None = None) -> EventRecord:
        self.webhook_count += 1
        self.accepted_count += 1
        record = event_record_from_parsed(parsed, event_id=event_id)
        self._records.append(record)
        self._event_ids.add(record.event_id)
        return record

    def record_duplicate(self) -> None:
        self.webhook_count += 1
        self.duplicate_count += 1

    def record_rejected(self) -> None:
        self.webhook_count += 1
        self.rejected_count += 1

    def recent_events(self) -> list[EventRecord]:
        return list(reversed(self._records))

    def debug_health(
        self,
        review_queue_counters: ReviewQueueCounters,
        accepted_count: int | None = None,
    ) -> DebugHealth:
        effective_accepted_count = self.accepted_count if accepted_count is None else accepted_count
        return DebugHealth(
            webhook_count=effective_accepted_count + self.rejected_count,
            accepted_count=effective_accepted_count,
            rejected_count=self.rejected_count,
            uptime=round(monotonic() - self._started_at, 3),
            **review_queue_counters.model_dump(),
        )

    def reset(self) -> None:
        self._records.clear()
        self._event_ids.clear()
        self._started_at = monotonic()
        self.webhook_count = 0
        self.accepted_count = 0
        self.rejected_count = 0
        self.duplicate_count = 0


def event_record_from_parsed(parsed: ParsedGitHubEvent, *, event_id: str | None = None) -> EventRecord:
    correlation_key = correlation_key_from_parsed(parsed)
    return EventRecord(
        event_id=event_id or str(uuid4()),
        github_event=parsed.event_type,
        diagnostic_stage="webhook_accepted",
        correlation_id=correlation_id_from_key(correlation_key),
        correlation_key=correlation_key,
        repo_full_name=parsed.repository,
        branch=branch_from_parsed(parsed),
        commit_sha=parsed.head_sha,
        issue_number=parsed.issue_number,
        pr_number=parsed.pull_request_number,
        received_at=datetime.now(UTC),
        raw_action=parsed.action,
    )


def webhook_delivery_key(parsed: ParsedGitHubEvent, delivery_id: str | None = None) -> str:
    if delivery_id and delivery_id.strip():
        return f"github-delivery:{delivery_id.strip()}"
    parts = [
        str(parsed.event_type),
        parsed.repository or "",
        parsed.action or "",
        parsed.action_label or "",
        branch_from_parsed(parsed) or "",
        parsed.head_sha or "",
        str(parsed.issue_number or ""),
        str(parsed.pull_request_number or ""),
    ]
    return "github-derived:" + ":".join(parts)


event_store = InMemoryEventStore()
