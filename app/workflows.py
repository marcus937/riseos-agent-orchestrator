from datetime import datetime

from pydantic import BaseModel, Field

from app.event_store import EventRecord
from app.review_queue import ReviewWorkItem
from app.workflow_lifecycle import (
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


def build_workflows(review_items: list[ReviewWorkItem], events: list[EventRecord]) -> list[WorkflowRecord]:
    workflows = [_workflow_from_item(item) for item in review_items]
    item_keys = {_workflow_identity_key(workflow) for workflow in workflows}
    for record in events:
        projection = build_event_workflow_projection(record)
        if not projection.workflow_state:
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
        blocked=sum(1 for workflow in workflows if workflow.current_state == WorkflowState.BLOCKED),
        reviewing=sum(1 for workflow in workflows if workflow.current_state in _REVIEWING_STATES),
        verified=sum(1 for workflow in workflows if workflow.current_state == WorkflowState.VERIFIED),
    )


def _workflow_from_item(item: ReviewWorkItem) -> WorkflowRecord:
    projection = build_work_item_workflow_projection(item)
    timeline = projection.workflow_events
    current_state = projection.workflow_state or WorkflowState.CREATED
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


def _workflow_from_event(record: EventRecord) -> WorkflowRecord:
    projection = build_event_workflow_projection(record)
    timeline = projection.workflow_events
    current_state = projection.workflow_state or WorkflowState.CREATED
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


def _workflow_identity_key(workflow: WorkflowRecord) -> tuple[str | None, int | None, int | None]:
    return (workflow.repo_full_name, workflow.issue_number, workflow.pr_number)


def _assigned_agent(item: ReviewWorkItem | None, state: WorkflowState) -> str | None:
    labels = set(item.labels if item is not None else [])
    if state in {WorkflowState.ASSIGNED, WorkflowState.CIRCUIT_WORKING} or labels & {"agent-ready", "agent-next"}:
        return "circuit-forge"
    return None


def _route_history_entry(event: WorkflowEvent) -> str:
    return f"{event.actor or event.owner.value}: {event.new_state or event.state}"


_TERMINAL_STATES = {WorkflowState.MERGED, WorkflowState.DEPLOYED, WorkflowState.VERIFIED}
_REVIEWING_STATES = {WorkflowState.HERMES_VALIDATING, WorkflowState.BB2_REVIEWING, WorkflowState.CHANGES_REQUESTED}
