from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.agent_tasks import AgentTask, AgentTaskStatus, AgentTaskWorkflowSummary
from app.event_store import EventRecord, EventWorkflowSummary
from app.review_queue import ReviewWorkItem, ReviewWorkItemWorkflowSummary
from app.workflow_lifecycle import (
    LegacyWorkflowState,
    WorkflowEvent,
    WorkflowOwner,
    WorkflowState,
    build_event_records_workflow_projection,
    build_event_workflow_projection,
    build_work_item_workflow_projection,
)

WORKFLOW_LIST_DEFAULT_LIMIT = 50
WORKFLOW_LIST_MAX_LIMIT = 100
WORKFLOW_LIST_DEFAULT_RECENT_DAYS = 14
WORKFLOW_LIST_MAX_RECENT_DAYS = 90


class WorkflowListFilter(StrEnum):
    ACTIVE_RECENT = "active_recent"
    ACTIVE = "active"
    RECENT = "recent"
    ALL = "all"


class WorkflowSummaryRecord(BaseModel):
    workflow_id: str
    correlation_id: str | None = None
    repo_full_name: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    agent_task_id: str | None = None
    current_state: WorkflowState
    assigned_agent: str | None = None
    hermes_job_id: str | None = None
    last_actor: str
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime


class WorkflowRecord(WorkflowSummaryRecord):
    timeline: list[WorkflowEvent] = Field(default_factory=list)
    route_history: list[str] = Field(default_factory=list)


class WorkflowPaginationMetadata(BaseModel):
    limit: int
    offset: int
    returned: int
    total: int
    unfiltered_total: int
    truncated: bool
    has_next: bool
    next_offset: int | None = None
    filter: WorkflowListFilter
    recent_days: int


class WorkflowCollection(BaseModel):
    workflows: list[WorkflowSummaryRecord]
    pagination: WorkflowPaginationMetadata | None = None


class WorkflowTimeline(BaseModel):
    workflow_id: str
    events: list[WorkflowEvent]


class WorkflowSummaryCounts(BaseModel):
    active: int = 0
    blocked: int = 0
    reviewing: int = 0
    verified: int = 0


def build_workflows(
    review_items: list[ReviewWorkItem],
    events: list[EventRecord],
    agent_tasks: list[AgentTask] | None = None,
) -> list[WorkflowRecord]:
    workflows = [_workflow_from_item(item) for item in review_items]
    workflows.extend(_workflow_from_agent_task(task) for task in (agent_tasks or []))
    review_item_keys = {_review_item_workflow_identity_key(item) for item in review_items}
    event_records_by_workflow_id: dict[str, list[EventRecord]] = {}
    for record in events:
        projection = build_event_workflow_projection(record)
        if not projection.canonical_workflow_state:
            continue
        event_records_by_workflow_id.setdefault(_event_workflow_id(record), []).append(record)
    for records in event_records_by_workflow_id.values():
        event_workflow = _workflow_from_events(records)
        if _event_records_workflow_identity_key(records) in review_item_keys:
            continue
        workflows.append(event_workflow)
    return sort_workflows(workflows)


ReviewWorkflowSummarySource = ReviewWorkItem | ReviewWorkItemWorkflowSummary


def build_workflow_summaries(
    review_items: list[ReviewWorkflowSummarySource],
    events: list[EventRecord | EventWorkflowSummary],
    agent_tasks: list[AgentTask | AgentTaskWorkflowSummary] | None = None,
) -> list[WorkflowSummaryRecord]:
    workflows = [workflow_summary_from_review_item(item) for item in review_items]
    workflows.extend(workflow_summary_from_agent_task(task) for task in (agent_tasks or []))
    review_item_keys = {_review_item_workflow_identity_key(item) for item in review_items}
    event_records_by_workflow_id: dict[str, list[EventRecord | EventWorkflowSummary]] = {}
    for record in events:
        projection = build_event_workflow_projection(record)
        if not projection.canonical_workflow_state:
            continue
        event_records_by_workflow_id.setdefault(_event_workflow_id(record), []).append(record)
    for records in event_records_by_workflow_id.values():
        if _event_records_workflow_identity_key(records) in review_item_keys:
            continue
        workflows.append(workflow_summary_from_event_records(records))
    return sort_workflow_summaries(workflows)


def build_workflow_collection(
    workflows: list[WorkflowRecord],
    *,
    limit: int = WORKFLOW_LIST_DEFAULT_LIMIT,
    offset: int = 0,
    workflow_filter: WorkflowListFilter = WorkflowListFilter.ACTIVE_RECENT,
    recent_days: int = WORKFLOW_LIST_DEFAULT_RECENT_DAYS,
    now: datetime | None = None,
) -> WorkflowCollection:
    bounded_limit = min(max(limit, 1), WORKFLOW_LIST_MAX_LIMIT)
    bounded_offset = max(offset, 0)
    bounded_recent_days = min(max(recent_days, 1), WORKFLOW_LIST_MAX_RECENT_DAYS)
    normalized_filter = WorkflowListFilter(workflow_filter)
    summaries = workflow_summaries(sort_workflows(workflows))
    filtered = filter_workflow_summaries(
        summaries,
        workflow_filter=normalized_filter,
        recent_days=bounded_recent_days,
        now=now,
    )
    page = filtered[bounded_offset : bounded_offset + bounded_limit]
    next_offset = (
        bounded_offset + bounded_limit
        if bounded_offset + bounded_limit < len(filtered)
        else None
    )
    return WorkflowCollection(
        workflows=page,
        pagination=WorkflowPaginationMetadata(
            limit=bounded_limit,
            offset=bounded_offset,
            returned=len(page),
            total=len(filtered),
            unfiltered_total=len(workflows),
            truncated=next_offset is not None,
            has_next=next_offset is not None,
            next_offset=next_offset,
            filter=normalized_filter,
            recent_days=bounded_recent_days,
        ),
    )


def filter_workflows(
    workflows: list[WorkflowRecord],
    *,
    workflow_filter: WorkflowListFilter = WorkflowListFilter.ACTIVE_RECENT,
    recent_days: int = WORKFLOW_LIST_DEFAULT_RECENT_DAYS,
    now: datetime | None = None,
) -> list[WorkflowRecord]:
    normalized_filter = WorkflowListFilter(workflow_filter)
    if normalized_filter == WorkflowListFilter.ALL:
        return workflows

    cutoff = _as_utc(now or datetime.now(UTC)) - timedelta(days=recent_days)
    return [workflow for workflow in workflows if _matches_workflow_filter(workflow, normalized_filter, cutoff)]


def filter_workflow_summaries(
    workflows: list[WorkflowSummaryRecord],
    *,
    workflow_filter: WorkflowListFilter = WorkflowListFilter.ACTIVE_RECENT,
    recent_days: int = WORKFLOW_LIST_DEFAULT_RECENT_DAYS,
    now: datetime | None = None,
) -> list[WorkflowSummaryRecord]:
    normalized_filter = WorkflowListFilter(workflow_filter)
    if normalized_filter == WorkflowListFilter.ALL:
        return workflows

    cutoff = _as_utc(now or datetime.now(UTC)) - timedelta(days=recent_days)
    return [workflow for workflow in workflows if _matches_workflow_filter(workflow, normalized_filter, cutoff)]


def sort_workflows(workflows: list[WorkflowRecord]) -> list[WorkflowRecord]:
    return sorted(
        sorted(workflows, key=lambda workflow: workflow.workflow_id),
        key=lambda workflow: (
            _as_utc(workflow.last_activity_at),
            _as_utc(workflow.updated_at),
            _as_utc(workflow.created_at),
        ),
        reverse=True,
    )


def sort_workflow_summaries(workflows: list[WorkflowSummaryRecord]) -> list[WorkflowSummaryRecord]:
    return sorted(
        sorted(workflows, key=lambda workflow: workflow.workflow_id),
        key=lambda workflow: (
            _as_utc(workflow.last_activity_at),
            _as_utc(workflow.updated_at),
            _as_utc(workflow.created_at),
        ),
        reverse=True,
    )


def find_workflow(workflows: list[WorkflowRecord], workflow_id: str) -> WorkflowRecord | None:
    return next((workflow for workflow in workflows if workflow.workflow_id == workflow_id), None)


def workflow_summary(workflow: WorkflowRecord) -> WorkflowSummaryRecord:
    return WorkflowSummaryRecord.model_validate(
        workflow.model_dump(exclude={"timeline", "route_history"})
    )


def workflow_summaries(workflows: list[WorkflowRecord]) -> list[WorkflowSummaryRecord]:
    return [workflow_summary(workflow) for workflow in workflows]


def build_workflow_summary_counts(workflows: list[WorkflowSummaryRecord]) -> WorkflowSummaryCounts:
    return WorkflowSummaryCounts(
        active=sum(1 for workflow in workflows if workflow.current_state not in _TERMINAL_STATES),
        blocked=sum(1 for workflow in workflows if workflow.current_state in _BLOCKED_STATES),
        reviewing=sum(1 for workflow in workflows if workflow.current_state in _REVIEWING_STATES),
        verified=sum(1 for workflow in workflows if workflow.current_state == WorkflowState.VERIFIED),
    )


def workflow_summary_from_review_item(item: ReviewWorkflowSummarySource) -> WorkflowSummaryRecord:
    projection = build_work_item_workflow_projection(item)
    timeline = projection.workflow_events
    current_state = projection.canonical_workflow_state or WorkflowState.CREATED
    created_at = timeline[0].occurred_at if timeline else item.created_at
    updated_at = (item.updated_at or timeline[-1].occurred_at) if timeline else item.created_at
    return WorkflowSummaryRecord(
        workflow_id=f"wf-{item.id}",
        repo_full_name=item.repo_full_name,
        issue_number=item.issue_number,
        pr_number=item.pr_number,
        current_state=current_state,
        assigned_agent=_assigned_agent(item, current_state),
        last_actor=(projection.current_owner or WorkflowOwner.UNKNOWN).value,
        created_at=created_at,
        updated_at=updated_at,
        last_activity_at=timeline[-1].occurred_at if timeline else updated_at,
    )


def workflow_summary_from_agent_task(
    task: AgentTask | AgentTaskWorkflowSummary,
) -> WorkflowSummaryRecord:
    current_state = _state_from_agent_task_status(task.status)
    lifecycle_events = getattr(task, "lifecycle_events", [])
    last_event = lifecycle_events[-1] if lifecycle_events else None
    activity_candidates = [_agent_task_last_activity_at(task)]
    if last_event is not None:
        activity_candidates.append(last_event.occurred_at)
    last_activity_at = max(activity_candidates, key=_as_utc)
    return WorkflowSummaryRecord(
        workflow_id=f"wf-agent-task-{task.task_id}",
        correlation_id=task.correlation_id,
        repo_full_name=task.repo_full_name,
        issue_number=task.issue_number,
        agent_task_id=task.task_id,
        current_state=current_state,
        assigned_agent=task.target_agent,
        last_actor=_agent_task_last_actor(task, current_state, last_event),
        created_at=task.created_at,
        updated_at=task.updated_at,
        last_activity_at=last_activity_at,
    )


def workflow_summary_from_event_records(
    records: list[EventRecord | EventWorkflowSummary],
) -> WorkflowSummaryRecord:
    ordered_records = sorted(records, key=lambda record: (record.received_at, record.event_id))
    projection = build_event_records_workflow_projection(ordered_records)
    current_state = projection.canonical_workflow_state or WorkflowState.CREATED
    last_record = ordered_records[-1]
    first_received_at = min(
        (getattr(record, "first_received_at", record.received_at) for record in ordered_records),
        key=_as_utc,
    )
    return WorkflowSummaryRecord(
        workflow_id=_event_workflow_id(last_record),
        correlation_id=_latest_record_value(ordered_records, "correlation_id"),
        repo_full_name=_latest_record_value(ordered_records, "repo_full_name"),
        issue_number=_latest_record_value(ordered_records, "issue_number"),
        pr_number=_latest_record_value(ordered_records, "pr_number"),
        current_state=current_state,
        assigned_agent=_assigned_agent(None, current_state),
        last_actor=(projection.current_owner or WorkflowOwner.UNKNOWN).value,
        created_at=first_received_at,
        updated_at=last_record.received_at,
        last_activity_at=last_record.received_at,
    )


def _workflow_from_item(item: ReviewWorkItem) -> WorkflowRecord:
    projection = build_work_item_workflow_projection(item)
    timeline = projection.workflow_events
    current_state = projection.canonical_workflow_state or WorkflowState.CREATED
    created_at = timeline[0].occurred_at if timeline else item.created_at
    updated_at = (item.updated_at or timeline[-1].occurred_at) if timeline else item.created_at
    return WorkflowRecord(
        workflow_id=f"wf-{item.id}",
        repo_full_name=item.repo_full_name,
        issue_number=item.issue_number,
        pr_number=item.pr_number,
        current_state=current_state,
        assigned_agent=_assigned_agent(item, current_state),
        last_actor=(projection.current_owner or WorkflowOwner.UNKNOWN).value,
        created_at=created_at,
        updated_at=updated_at,
        last_activity_at=timeline[-1].occurred_at if timeline else updated_at,
        timeline=timeline,
        route_history=[_route_history_entry(event) for event in timeline],
    )


def _workflow_from_agent_task(task: AgentTask) -> WorkflowRecord:
    timeline = _agent_task_events(task)
    current_state = _state_from_agent_task_status(task.status)
    last_event = timeline[-1]
    return WorkflowRecord(
        workflow_id=f"wf-agent-task-{task.task_id}",
        correlation_id=task.correlation_id,
        repo_full_name=task.repo_full_name,
        issue_number=task.issue_number,
        agent_task_id=task.task_id,
        current_state=current_state,
        assigned_agent=task.target_agent,
        last_actor=last_event.actor or WorkflowOwner.ORCHESTRATOR.value,
        created_at=task.created_at,
        updated_at=task.updated_at,
        last_activity_at=last_event.occurred_at,
        timeline=timeline,
        route_history=[_route_history_entry(event) for event in timeline],
    )


def _workflow_from_events(records: list[EventRecord]) -> WorkflowRecord:
    ordered_records = sorted(records, key=lambda record: (record.received_at, record.event_id))
    projection = build_event_records_workflow_projection(ordered_records)
    timeline = projection.workflow_events
    current_state = projection.canonical_workflow_state or WorkflowState.CREATED
    first_record = ordered_records[0]
    last_record = ordered_records[-1]
    return WorkflowRecord(
        workflow_id=_event_workflow_id(last_record),
        correlation_id=_latest_record_value(ordered_records, "correlation_id"),
        repo_full_name=_latest_record_value(ordered_records, "repo_full_name"),
        issue_number=_latest_record_value(ordered_records, "issue_number"),
        pr_number=_latest_record_value(ordered_records, "pr_number"),
        current_state=current_state,
        assigned_agent=_assigned_agent(None, current_state),
        last_actor=(projection.current_owner or WorkflowOwner.UNKNOWN).value,
        created_at=first_record.received_at,
        updated_at=last_record.received_at,
        last_activity_at=last_record.received_at,
        timeline=timeline,
        route_history=[_route_history_entry(event) for event in timeline],
    )


def _agent_task_events(task: AgentTask) -> list[WorkflowEvent]:
    events: list[WorkflowEvent] = []
    for lifecycle_event in task.lifecycle_events:
        state = _state_from_agent_task_event(lifecycle_event.event, task.status)
        events.append(
            WorkflowEvent(
                state=_legacy_state_from_agent_task_state(state),
                canonical_state=state,
                occurred_at=lifecycle_event.occurred_at,
                owner=_owner_from_agent_task_state(state),
                source="agent_task",
                event_type="agent_task.lifecycle.changed",
                actor=lifecycle_event.actor,
                item_id=task.task_id,
                repo_full_name=task.repo_full_name,
                issue_number=task.issue_number,
                branch=task.branch,
                commit_sha=task.commit_sha,
                metadata={
                    "agent_task_id": task.task_id,
                    "agent_task_event": lifecycle_event.event,
                    "title": task.title,
                    "target_agent": task.target_agent,
                    "priority": task.priority.value,
                    "agent_bus_work_item_id": task.agent_bus_work_item_id,
                    **lifecycle_event.metadata,
                },
            )
        )
    if events:
        return events
    state = _state_from_agent_task_status(task.status)
    return [
        WorkflowEvent(
            state=_legacy_state_from_agent_task_state(state),
            canonical_state=state,
            occurred_at=task.created_at,
            owner=_owner_from_agent_task_state(state),
            source="agent_task",
            event_type="agent_task.lifecycle.changed",
            item_id=task.task_id,
            repo_full_name=task.repo_full_name,
            issue_number=task.issue_number,
            branch=task.branch,
            commit_sha=task.commit_sha,
            metadata={"agent_task_id": task.task_id, "target_agent": task.target_agent},
        )
    ]


def _state_from_agent_task_event(event: str, fallback_status: AgentTaskStatus) -> WorkflowState:
    if event == "created":
        return WorkflowState.CREATED
    if event in {"queued", "assigned"}:
        return WorkflowState.ASSIGNED
    if event in {"claimed", "running", "in_progress"}:
        return WorkflowState.CIRCUIT_WORKING
    if event == "ready_for_review":
        return WorkflowState.BB2_REVIEWING
    if event == "completed":
        return WorkflowState.COMPLETED
    if event in {"failed", "cancelled", "agent_bus_dispatch_failed"}:
        return WorkflowState.BLOCKED
    return _state_from_agent_task_status(fallback_status)


def _state_from_agent_task_status(status: AgentTaskStatus) -> WorkflowState:
    if status == AgentTaskStatus.CREATED:
        return WorkflowState.CREATED
    if status in {AgentTaskStatus.QUEUED, AgentTaskStatus.ASSIGNED}:
        return WorkflowState.ASSIGNED
    if status in {AgentTaskStatus.CLAIMED, AgentTaskStatus.RUNNING, AgentTaskStatus.IN_PROGRESS}:
        return WorkflowState.CIRCUIT_WORKING
    if status == AgentTaskStatus.READY_FOR_REVIEW:
        return WorkflowState.BB2_REVIEWING
    if status == AgentTaskStatus.COMPLETED:
        return WorkflowState.COMPLETED
    if status in {AgentTaskStatus.FAILED, AgentTaskStatus.CANCELLED}:
        return WorkflowState.BLOCKED
    return WorkflowState.CREATED


def _legacy_state_from_agent_task_state(state: WorkflowState) -> LegacyWorkflowState:
    if state == WorkflowState.CREATED:
        return LegacyWorkflowState.ISSUE_CREATED
    if state == WorkflowState.ASSIGNED:
        return LegacyWorkflowState.AGENT_READY
    if state == WorkflowState.CIRCUIT_WORKING:
        return LegacyWorkflowState.CIRCUIT_IN_PROGRESS
    if state == WorkflowState.BB2_REVIEWING:
        return LegacyWorkflowState.BB2_REVIEW_REQUESTED
    if state == WorkflowState.COMPLETED:
        return LegacyWorkflowState.COMPLETED
    if state == WorkflowState.BLOCKED:
        return LegacyWorkflowState.BLOCKED
    return LegacyWorkflowState.ISSUE_CREATED


def _owner_from_agent_task_state(state: WorkflowState) -> WorkflowOwner:
    if state == WorkflowState.CIRCUIT_WORKING:
        return WorkflowOwner.CIRCUIT
    if state == WorkflowState.BB2_REVIEWING:
        return WorkflowOwner.BB2
    if state == WorkflowState.COMPLETED:
        return WorkflowOwner.HUMAN
    if state == WorkflowState.BLOCKED:
        return WorkflowOwner.ORCHESTRATOR
    return WorkflowOwner.ORCHESTRATOR


def _agent_task_last_activity_at(task: AgentTask | AgentTaskWorkflowSummary) -> datetime:
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
    return max(values, key=_as_utc)


def _agent_task_last_actor(
    task: AgentTask | AgentTaskWorkflowSummary,
    state: WorkflowState,
    last_event: Any | None,
) -> str:
    if last_event is not None and last_event.actor:
        return last_event.actor
    last_actor = getattr(task, "last_actor", None)
    if last_actor:
        return last_actor
    if state in {WorkflowState.CIRCUIT_WORKING, WorkflowState.COMPLETED, WorkflowState.BLOCKED}:
        return task.target_agent
    if state == WorkflowState.BB2_REVIEWING:
        return WorkflowOwner.BB2.value
    return WorkflowOwner.ORCHESTRATOR.value


def _review_item_workflow_identity_key(
    item: ReviewWorkflowSummarySource,
) -> tuple[str, str | None, int | None, int | None, str | None, str | None]:
    return _github_workflow_identity_key(
        repo_full_name=item.repo_full_name,
        issue_number=item.issue_number,
        pr_number=item.pr_number,
        branch=item.branch,
        commit_sha=item.commit_sha,
        fallback_id=item.id,
    )


def _event_records_workflow_identity_key(
    records: list[EventRecord | EventWorkflowSummary],
) -> tuple[str, str | None, int | None, int | None, str | None, str | None]:
    ordered_records = sorted(records, key=lambda record: (record.received_at, record.event_id))
    for record in reversed(ordered_records):
        if record.issue_number is not None or record.pr_number is not None:
            return _event_record_workflow_identity_key(record)
    return _event_record_workflow_identity_key(ordered_records[-1])


def _event_record_workflow_identity_key(
    record: EventRecord | EventWorkflowSummary,
) -> tuple[str, str | None, int | None, int | None, str | None, str | None]:
    return _github_workflow_identity_key(
        repo_full_name=record.repo_full_name,
        issue_number=record.issue_number,
        pr_number=record.pr_number,
        branch=record.branch,
        commit_sha=record.commit_sha,
        fallback_id=record.correlation_id or record.event_id,
    )


def _github_workflow_identity_key(
    *,
    repo_full_name: str | None,
    issue_number: int | None,
    pr_number: int | None,
    branch: str | None,
    commit_sha: str | None,
    fallback_id: str,
) -> tuple[str, str | None, int | None, int | None, str | None, str | None]:
    if issue_number is not None or pr_number is not None:
        return ("github_subject", repo_full_name, issue_number, pr_number, None, None)
    if branch is not None or commit_sha is not None:
        return ("github_ref", repo_full_name, None, None, branch, commit_sha)
    return ("github_record", repo_full_name, None, None, fallback_id, None)


def _event_workflow_id(record: EventRecord | EventWorkflowSummary) -> str:
    workflow_id = getattr(record, "workflow_id", None)
    if workflow_id:
        return workflow_id
    return f"wf-{record.correlation_id or record.event_id}"


def _latest_record_value(records: list[EventRecord | EventWorkflowSummary], name: str) -> Any:
    for record in reversed(records):
        value = getattr(record, name)
        if value is not None:
            return value
    return None


def _assigned_agent(item: ReviewWorkflowSummarySource | None, state: WorkflowState) -> str | None:
    labels = set(item.labels if item is not None else [])
    if state in {WorkflowState.ASSIGNED, WorkflowState.CIRCUIT_WORKING} or labels & {"agent-ready", "agent-next"}:
        return "circuit-forge"
    return None


def _route_history_entry(event: WorkflowEvent) -> str:
    return f"{event.actor or event.owner.value}: {event.new_state or event.canonical_state}"


def _matches_workflow_filter(workflow: WorkflowRecord, workflow_filter: WorkflowListFilter, cutoff: datetime) -> bool:
    active = workflow.current_state not in _TERMINAL_STATES
    recent = _as_utc(workflow.last_activity_at) >= cutoff
    if workflow_filter == WorkflowListFilter.ACTIVE:
        return active
    if workflow_filter == WorkflowListFilter.RECENT:
        return recent
    return active or recent


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


_TERMINAL_STATES = {
    WorkflowState.COMPLETED,
    WorkflowState.MERGED,
    WorkflowState.CLOSED_UNMERGED,
    WorkflowState.ABANDONED,
    WorkflowState.DEPLOYED,
    WorkflowState.VERIFIED,
}
_BLOCKED_STATES = {
    WorkflowState.BLOCKED,
    WorkflowState.HERMES_FAILED,
    WorkflowState.CLOSED_UNMERGED,
    WorkflowState.ABANDONED,
}
_REVIEWING_STATES = {
    WorkflowState.HERMES_VALIDATING,
    WorkflowState.BB2_REVIEWING,
    WorkflowState.CHANGES_REQUESTED,
}
