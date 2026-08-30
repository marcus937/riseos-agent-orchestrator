from datetime import datetime

from pydantic import BaseModel, Field

from app.agent_tasks import AgentTask, AgentTaskStatus
from app.event_store import EventRecord
from app.review_queue import ReviewWorkItem
from app.workflow_lifecycle import (
    LegacyWorkflowState,
    WorkflowEvent,
    WorkflowOwner,
    WorkflowState,
    build_event_workflow_projection,
    build_work_item_workflow_projection,
)


class WorkflowRecord(BaseModel):
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
    timeline: list[WorkflowEvent] = Field(default_factory=list)
    route_history: list[str] = Field(default_factory=list)


class WorkflowCollection(BaseModel):
    workflows: list[WorkflowRecord]


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
    item_keys = {_workflow_identity_key(workflow) for workflow in workflows}
    for record in events:
        projection = build_event_workflow_projection(record)
        if not projection.canonical_workflow_state:
            continue
        event_workflow = _workflow_from_event(record)
        if _workflow_identity_key(event_workflow) in item_keys:
            continue
        workflows.append(event_workflow)
    return sorted(workflows, key=lambda workflow: workflow.last_activity_at, reverse=True)


def find_workflow(workflows: list[WorkflowRecord], workflow_id: str) -> WorkflowRecord | None:
    return next((workflow for workflow in workflows if workflow.workflow_id == workflow_id), None)


def build_workflow_summary_counts(workflows: list[WorkflowRecord]) -> WorkflowSummaryCounts:
    return WorkflowSummaryCounts(
        active=sum(1 for workflow in workflows if workflow.current_state not in _TERMINAL_STATES),
        blocked=sum(1 for workflow in workflows if workflow.current_state in _BLOCKED_STATES),
        reviewing=sum(1 for workflow in workflows if workflow.current_state in _REVIEWING_STATES),
        verified=sum(1 for workflow in workflows if workflow.current_state == WorkflowState.VERIFIED),
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


def _workflow_from_event(record: EventRecord) -> WorkflowRecord:
    projection = build_event_workflow_projection(record)
    timeline = projection.workflow_events
    current_state = projection.canonical_workflow_state or WorkflowState.CREATED
    workflow_id = f"wf-{record.correlation_id or record.event_id}"
    return WorkflowRecord(
        workflow_id=workflow_id,
        correlation_id=record.correlation_id,
        repo_full_name=record.repo_full_name,
        issue_number=record.issue_number,
        pr_number=record.pr_number,
        current_state=current_state,
        assigned_agent=_assigned_agent(None, current_state),
        last_actor=(projection.current_owner or WorkflowOwner.UNKNOWN).value,
        created_at=record.received_at,
        updated_at=record.received_at,
        last_activity_at=record.received_at,
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


def _workflow_identity_key(workflow: WorkflowRecord) -> tuple[str | None, int | None, int | None, str | None]:
    return (workflow.repo_full_name, workflow.issue_number, workflow.pr_number, workflow.agent_task_id)


def _assigned_agent(item: ReviewWorkItem | None, state: WorkflowState) -> str | None:
    labels = set(item.labels if item is not None else [])
    if state in {WorkflowState.ASSIGNED, WorkflowState.CIRCUIT_WORKING} or labels & {"agent-ready", "agent-next"}:
        return "circuit-forge"
    return None


def _route_history_entry(event: WorkflowEvent) -> str:
    return f"{event.actor or event.owner.value}: {event.new_state or event.canonical_state}"


_TERMINAL_STATES = {
    WorkflowState.COMPLETED,
    WorkflowState.MERGED,
    WorkflowState.CLOSED_UNMERGED,
    WorkflowState.ABANDONED,
    WorkflowState.DEPLOYED,
    WorkflowState.VERIFIED,
}
_BLOCKED_STATES = {WorkflowState.BLOCKED, WorkflowState.HERMES_FAILED, WorkflowState.CLOSED_UNMERGED, WorkflowState.ABANDONED}
_REVIEWING_STATES = {WorkflowState.HERMES_VALIDATING, WorkflowState.BB2_REVIEWING, WorkflowState.CHANGES_REQUESTED}
