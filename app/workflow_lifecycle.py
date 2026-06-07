from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.event_store import EventRecord
from app.github_events import GitHubEventType
from app.review_queue import ReviewLifecycleStage, ReviewWorkItem, ReviewWorkItemStatus


class WorkflowState(StrEnum):
    ISSUE_CREATED = "ISSUE_CREATED"
    AGENT_READY = "AGENT_READY"
    CIRCUIT_CLAIMED = "CIRCUIT_CLAIMED"
    CIRCUIT_IN_PROGRESS = "CIRCUIT_IN_PROGRESS"
    PR_OPENED = "PR_OPENED"
    HERMES_VALIDATION_REQUESTED = "HERMES_VALIDATION_REQUESTED"
    HERMES_VALIDATION_RUNNING = "HERMES_VALIDATION_RUNNING"
    HERMES_VALIDATION_PASSED = "HERMES_VALIDATION_PASSED"
    BB2_REVIEW_REQUESTED = "BB2_REVIEW_REQUESTED"
    BB2_NEEDS_CHANGES = "BB2_NEEDS_CHANGES"
    CIRCUIT_REWORK = "CIRCUIT_REWORK"
    HERMES_REVALIDATION = "HERMES_REVALIDATION"
    BB2_APPROVED = "BB2_APPROVED"
    READY_TO_MERGE = "READY_TO_MERGE"
    MERGED = "MERGED"
    DEPLOYED = "DEPLOYED"


class WorkflowOwner(StrEnum):
    ORCHESTRATOR = "Orchestrator"
    CIRCUIT = "Circuit"
    HERMES = "Hermes"
    BB2 = "BB2"
    HUMAN = "Human"
    UNKNOWN = "Unknown"


class WorkflowEvent(BaseModel):
    state: WorkflowState
    occurred_at: datetime
    owner: WorkflowOwner
    source: str
    previous_state: WorkflowState | None = None
    duration_seconds: float | None = None
    item_id: str | None = None
    repo_full_name: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    branch: str | None = None
    commit_sha: str | None = None
    github_event: GitHubEventType | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowStateProjection(BaseModel):
    workflow_events: list[WorkflowEvent] = Field(default_factory=list)
    workflow_state: WorkflowState | None = None
    workflow_state_history: list[WorkflowEvent] = Field(default_factory=list)
    workflow_duration_seconds: float | None = None
    current_owner: WorkflowOwner = WorkflowOwner.UNKNOWN


def build_work_item_workflow_projection(item: ReviewWorkItem) -> WorkflowStateProjection:
    events = _dedupe_events(
        [
            _initial_work_item_event(item),
            *_review_lifecycle_events(item),
            *_terminal_status_events(item),
        ]
    )
    return _projection_from_events(events)


def build_event_workflow_projection(record: EventRecord) -> WorkflowStateProjection:
    state = _state_from_event_record(record)
    if state is None:
        return WorkflowStateProjection()
    event = WorkflowEvent(
        state=state,
        occurred_at=record.received_at,
        owner=_owner_for_state(state),
        source="github_webhook",
        repo_full_name=record.repo_full_name,
        issue_number=record.issue_number,
        pr_number=record.pr_number,
        branch=record.branch,
        commit_sha=record.commit_sha,
        github_event=record.github_event,
        metadata={"raw_action": record.raw_action} if record.raw_action else {},
    )
    return _projection_from_events([event])


def _initial_work_item_event(item: ReviewWorkItem) -> WorkflowEvent:
    state = _initial_state_for_item(item)
    return WorkflowEvent(
        state=state,
        occurred_at=item.created_at,
        owner=_owner_for_state(state),
        source="review_work_item",
        item_id=item.id,
        repo_full_name=item.repo_full_name,
        issue_number=item.issue_number,
        pr_number=item.pr_number,
        branch=item.branch,
        commit_sha=item.commit_sha,
        github_event=item.event_type,
        metadata={"labels": item.labels},
    )


def _review_lifecycle_events(item: ReviewWorkItem) -> list[WorkflowEvent]:
    stage_times: list[tuple[ReviewLifecycleStage, datetime | None]] = [
        (ReviewLifecycleStage.WORKER_CLAIMED, item.worker_claimed_at),
        (ReviewLifecycleStage.REVIEW_STARTED, item.review_started_at),
        (ReviewLifecycleStage.OPENAI_REVIEW_ATTEMPTED, item.openai_review_attempted_at),
        (ReviewLifecycleStage.OPENAI_REVIEW_SUCCEEDED, item.openai_review_completed_at),
        (ReviewLifecycleStage.OPENAI_REVIEW_FAILED, item.openai_review_completed_at),
        (ReviewLifecycleStage.GITHUB_WRITEBACK_STARTED, item.github_writeback_started_at),
        (ReviewLifecycleStage.GITHUB_WRITEBACK_COMPLETED, item.github_writeback_completed_at),
        (ReviewLifecycleStage.REVIEW_COMPLETED, item.review_completed_at),
        (ReviewLifecycleStage.REVIEW_FAILED, item.last_failure_at),
    ]
    events: list[WorkflowEvent] = []
    for stage, occurred_at in stage_times:
        if occurred_at is None:
            continue
        state = _state_from_review_stage(stage, item)
        if state is None:
            continue
        events.append(
            WorkflowEvent(
                state=state,
                occurred_at=occurred_at,
                owner=_owner_for_state(state),
                source="review_lifecycle",
                item_id=item.id,
                repo_full_name=item.repo_full_name,
                issue_number=item.issue_number,
                pr_number=item.pr_number,
                branch=item.branch,
                commit_sha=item.commit_sha,
                github_event=item.event_type,
                metadata={"review_lifecycle_stage": stage.value},
            )
        )
    return events


def _terminal_status_events(item: ReviewWorkItem) -> list[WorkflowEvent]:
    occurred_at = item.updated_at or item.review_completed_at
    if occurred_at is None:
        return []
    state = _state_from_work_item_status(item.status)
    if state is None:
        return []
    return [
        WorkflowEvent(
            state=state,
            occurred_at=occurred_at,
            owner=_owner_for_state(state),
            source="review_status",
            item_id=item.id,
            repo_full_name=item.repo_full_name,
            issue_number=item.issue_number,
            pr_number=item.pr_number,
            branch=item.branch,
            commit_sha=item.commit_sha,
            github_event=item.event_type,
            metadata={"review_status": item.status.value},
        )
    ]


def _projection_from_events(events: list[WorkflowEvent]) -> WorkflowStateProjection:
    ordered = sorted(events, key=lambda event: event.occurred_at)
    previous: WorkflowEvent | None = None
    for event in ordered:
        if previous is not None:
            event.previous_state = previous.state
            event.duration_seconds = round((event.occurred_at - previous.occurred_at).total_seconds(), 3)
        previous = event
    current = ordered[-1] if ordered else None
    duration = None
    if ordered:
        duration = round((datetime.now(UTC) - ordered[0].occurred_at).total_seconds(), 3)
    return WorkflowStateProjection(
        workflow_events=ordered,
        workflow_state=current.state if current else None,
        workflow_state_history=ordered,
        workflow_duration_seconds=duration,
        current_owner=current.owner if current else WorkflowOwner.UNKNOWN,
    )


def _dedupe_events(events: list[WorkflowEvent]) -> list[WorkflowEvent]:
    deduped: list[WorkflowEvent] = []
    seen: set[tuple[WorkflowState, datetime, str]] = set()
    for event in events:
        key = (event.state, event.occurred_at, event.source)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped


def _initial_state_for_item(item: ReviewWorkItem) -> WorkflowState:
    if item.pr_number is not None or item.event_type == GitHubEventType.PULL_REQUEST:
        return WorkflowState.PR_OPENED
    if "agent-ready" in item.labels or "agent-next" in item.labels:
        return WorkflowState.AGENT_READY
    if item.commit_sha:
        return WorkflowState.CIRCUIT_IN_PROGRESS
    return WorkflowState.ISSUE_CREATED


def _state_from_review_stage(stage: ReviewLifecycleStage, item: ReviewWorkItem) -> WorkflowState | None:
    if stage == ReviewLifecycleStage.WORKER_CLAIMED:
        return WorkflowState.HERMES_VALIDATION_REQUESTED
    if stage == ReviewLifecycleStage.REVIEW_STARTED:
        return WorkflowState.HERMES_VALIDATION_RUNNING
    if stage == ReviewLifecycleStage.OPENAI_REVIEW_ATTEMPTED:
        return WorkflowState.BB2_REVIEW_REQUESTED
    if stage == ReviewLifecycleStage.OPENAI_REVIEW_SUCCEEDED:
        return _state_from_work_item_status(item.status) or WorkflowState.BB2_APPROVED
    if stage == ReviewLifecycleStage.OPENAI_REVIEW_FAILED:
        return WorkflowState.BB2_NEEDS_CHANGES
    if stage == ReviewLifecycleStage.GITHUB_WRITEBACK_STARTED:
        return WorkflowState.BB2_REVIEW_REQUESTED
    if stage == ReviewLifecycleStage.GITHUB_WRITEBACK_COMPLETED:
        return WorkflowState.READY_TO_MERGE if item.github_writeback_success else WorkflowState.BB2_NEEDS_CHANGES
    if stage == ReviewLifecycleStage.REVIEW_COMPLETED:
        return _state_from_work_item_status(item.status)
    if stage == ReviewLifecycleStage.REVIEW_FAILED:
        return WorkflowState.BB2_NEEDS_CHANGES
    return None


def _state_from_work_item_status(status: ReviewWorkItemStatus) -> WorkflowState | None:
    if status == ReviewWorkItemStatus.PENDING_REVIEW:
        return None
    if status == ReviewWorkItemStatus.REVIEWING:
        return WorkflowState.HERMES_VALIDATION_RUNNING
    if status == ReviewWorkItemStatus.NEEDS_CHANGES:
        return WorkflowState.BB2_NEEDS_CHANGES
    if status == ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW:
        return WorkflowState.READY_TO_MERGE
    if status == ReviewWorkItemStatus.BLOCKED:
        return WorkflowState.BB2_NEEDS_CHANGES
    return None


def _state_from_event_record(record: EventRecord) -> WorkflowState | None:
    if record.github_event == GitHubEventType.ISSUES:
        if record.raw_action == "opened":
            return WorkflowState.ISSUE_CREATED
        if record.raw_action in {"labeled", "edited", "reopened"}:
            return WorkflowState.AGENT_READY
    if record.github_event == GitHubEventType.PULL_REQUEST:
        if record.raw_action in {"opened", "reopened", "synchronize"}:
            return WorkflowState.PR_OPENED
        if record.raw_action == "closed":
            return WorkflowState.MERGED
    if record.github_event == GitHubEventType.PULL_REQUEST_REVIEW:
        if record.raw_action == "submitted":
            return WorkflowState.BB2_REVIEW_REQUESTED
    if record.github_event == GitHubEventType.PUSH:
        return WorkflowState.CIRCUIT_IN_PROGRESS
    return None


def _owner_for_state(state: WorkflowState) -> WorkflowOwner:
    if state in {
        WorkflowState.CIRCUIT_CLAIMED,
        WorkflowState.CIRCUIT_IN_PROGRESS,
        WorkflowState.CIRCUIT_REWORK,
    }:
        return WorkflowOwner.CIRCUIT
    if state in {
        WorkflowState.HERMES_VALIDATION_REQUESTED,
        WorkflowState.HERMES_VALIDATION_RUNNING,
        WorkflowState.HERMES_VALIDATION_PASSED,
        WorkflowState.HERMES_REVALIDATION,
    }:
        return WorkflowOwner.HERMES
    if state in {
        WorkflowState.BB2_REVIEW_REQUESTED,
        WorkflowState.BB2_NEEDS_CHANGES,
        WorkflowState.BB2_APPROVED,
    }:
        return WorkflowOwner.BB2
    if state in {WorkflowState.READY_TO_MERGE, WorkflowState.MERGED, WorkflowState.DEPLOYED}:
        return WorkflowOwner.HUMAN
    return WorkflowOwner.ORCHESTRATOR
