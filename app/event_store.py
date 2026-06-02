from collections import deque
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from pydantic import BaseModel

from app.github_events import GitHubEventType, ParsedGitHubEvent


class EventRecord(BaseModel):
    event_id: str
    github_event: GitHubEventType
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


class InMemoryEventStore:
    def __init__(self, max_records: int = 50) -> None:
        self._records: deque[EventRecord] = deque(maxlen=max_records)
        self._started_at = monotonic()
        self.webhook_count = 0
        self.accepted_count = 0
        self.rejected_count = 0

    def record_accepted(self, parsed: ParsedGitHubEvent) -> EventRecord:
        self.webhook_count += 1
        self.accepted_count += 1
        record = event_record_from_parsed(parsed)
        self._records.append(record)
        return record

    def record_rejected(self) -> None:
        self.webhook_count += 1
        self.rejected_count += 1

    def recent_events(self) -> list[EventRecord]:
        return list(reversed(self._records))

    def debug_health(self) -> DebugHealth:
        return DebugHealth(
            webhook_count=self.webhook_count,
            accepted_count=self.accepted_count,
            rejected_count=self.rejected_count,
            uptime=round(monotonic() - self._started_at, 3),
        )

    def reset(self) -> None:
        self._records.clear()
        self._started_at = monotonic()
        self.webhook_count = 0
        self.accepted_count = 0
        self.rejected_count = 0


def event_record_from_parsed(parsed: ParsedGitHubEvent) -> EventRecord:
    return EventRecord(
        event_id=str(uuid4()),
        github_event=parsed.event_type,
        repo_full_name=parsed.repository,
        branch=_branch_from_parsed(parsed),
        commit_sha=parsed.head_sha,
        issue_number=parsed.issue_number,
        pr_number=parsed.pull_request_number,
        received_at=datetime.now(UTC),
        raw_action=parsed.action,
    )


def _branch_from_parsed(parsed: ParsedGitHubEvent) -> str | None:
    if parsed.head_ref:
        return parsed.head_ref
    if parsed.ref and parsed.ref.startswith("refs/heads/"):
        return parsed.ref.removeprefix("refs/heads/")
    return parsed.ref


event_store = InMemoryEventStore()
