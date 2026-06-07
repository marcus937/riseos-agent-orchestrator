from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.config import Settings
from app.event_store import DebugHealth, EventRecord
from app.review_queue import (
    RecentFailure,
    ReviewLifecycleVisibility,
    ReviewQueueStats,
    ReviewWorkItem,
    WorkerStats,
)
from app.workflow_lifecycle import (
    WorkflowEvent,
    WorkflowOwner,
    WorkflowState,
    build_event_workflow_projection,
    build_work_item_workflow_projection,
)

ORCHESTRATOR_SNAPSHOT_SCHEMA_VERSION = "orchestrator.snapshot.v1"


class OrchestratorSnapshotOverview(BaseModel):
    status: str
    app_env: str
    work_branch: str
    base_branch: str
    webhook_count: int
    accepted_count: int
    rejected_count: int
    review_queue_count: int
    pending_review_count: int
    active_reviewing_count: int
    approved_for_human_review_count: int
    blocked_count: int
    recent_failure_count: int


class WorkflowFields(BaseModel):
    workflow_events: list[WorkflowEvent] = Field(default_factory=list)
    workflow_state: WorkflowState | None = None
    workflow_state_history: list[WorkflowEvent] = Field(default_factory=list)
    workflow_duration_seconds: float | None = None
    current_owner: WorkflowOwner = WorkflowOwner.UNKNOWN


class WorkflowWorkItemSnapshot(ReviewWorkItem, WorkflowFields):
    pass


class WorkflowLifecycleVisibilitySnapshot(ReviewLifecycleVisibility, WorkflowFields):
    pass


class WorkflowEventRecordSnapshot(EventRecord, WorkflowFields):
    pass


class OrchestratorWorkforceSnapshot(BaseModel):
    overview: OrchestratorSnapshotOverview
    agents: list[WorkflowLifecycleVisibilitySnapshot]
    issues: list[WorkflowWorkItemSnapshot]
    prs: list[WorkflowWorkItemSnapshot]
    events: list[WorkflowEventRecordSnapshot]


class HermesDispatchStatus(BaseModel):
    default_target_configured: bool
    m2_dispatch_enabled: bool
    m2_configured: bool
    dgx_dispatch_enabled: bool
    dgx_configured: bool


class OrchestratorRuntime(BaseModel):
    auto_processing_enabled: bool
    github_context_hydration_enabled: bool
    github_writeback_enabled: bool
    task_dispatch_enabled: bool
    debug_reads_require_admin_token: bool
    hermes_dispatch: HermesDispatchStatus


class OrchestratorSnapshot(BaseModel):
    schema_version: str = ORCHESTRATOR_SNAPSHOT_SCHEMA_VERSION
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    workforce: OrchestratorWorkforceSnapshot
    queue: ReviewQueueStats
    health: DebugHealth
    runtime: OrchestratorRuntime
    recent_failures: list[RecentFailure] = Field(default_factory=list)


def build_orchestrator_snapshot(
    *,
    settings: Settings,
    health: DebugHealth,
    queue: ReviewQueueStats,
    worker_stats: WorkerStats,
    lifecycle: list[ReviewLifecycleVisibility],
    review_items: list[ReviewWorkItem],
    events: list[EventRecord],
    recent_failures: list[RecentFailure],
) -> OrchestratorSnapshot:
    workflow_items = [_workflow_work_item_snapshot(item) for item in review_items]
    return OrchestratorSnapshot(
        workforce=OrchestratorWorkforceSnapshot(
            overview=OrchestratorSnapshotOverview(
                status="ok",
                app_env=settings.app_env,
                work_branch=settings.work_branch,
                base_branch=settings.base_branch,
                webhook_count=health.webhook_count,
                accepted_count=health.accepted_count,
                rejected_count=health.rejected_count,
                review_queue_count=health.review_queue_count,
                pending_review_count=health.pending_review_count,
                active_reviewing_count=worker_stats.active_reviewing_count,
                approved_for_human_review_count=health.approved_for_human_review_count,
                blocked_count=health.blocked_count,
                recent_failure_count=queue.recent_failure_count,
            ),
            agents=[_workflow_lifecycle_snapshot(item, workflow_items) for item in lifecycle],
            issues=[item for item in workflow_items if item.issue_number is not None and item.pr_number is None],
            prs=[item for item in workflow_items if item.pr_number is not None],
            events=[_workflow_event_snapshot(event) for event in events],
        ),
        queue=queue,
        health=health,
        runtime=OrchestratorRuntime(
            auto_processing_enabled=settings.enable_auto_review_processing,
            github_context_hydration_enabled=settings.enable_github_context_hydration,
            github_writeback_enabled=settings.enable_github_writeback,
            task_dispatch_enabled=settings.enable_task_dispatch,
            debug_reads_require_admin_token=settings.require_admin_token_for_debug_reads,
            hermes_dispatch=build_hermes_dispatch_status(settings),
        ),
        recent_failures=recent_failures,
    )


def build_hermes_dispatch_status(settings: Settings) -> HermesDispatchStatus:
    return HermesDispatchStatus(
        default_target_configured=_configured_url(settings.hermes_default_target),
        m2_dispatch_enabled=settings.hermes_m2_enable_dispatch,
        m2_configured=bool(settings.hermes_m2_base_url and settings.hermes_m2_token),
        dgx_dispatch_enabled=settings.hermes_dgx_enable_dispatch,
        dgx_configured=bool(settings.hermes_dgx_base_url and settings.hermes_dgx_token),
    )


def snapshot_schema() -> dict[str, Any]:
    return OrchestratorSnapshot.model_json_schema()


def _workflow_work_item_snapshot(item: ReviewWorkItem) -> WorkflowWorkItemSnapshot:
    projection = build_work_item_workflow_projection(item)
    return WorkflowWorkItemSnapshot.model_validate(
        {
            **item.model_dump(),
            **projection.model_dump(),
        }
    )


def _workflow_lifecycle_snapshot(
    item: ReviewLifecycleVisibility,
    workflow_items: list[WorkflowWorkItemSnapshot],
) -> WorkflowLifecycleVisibilitySnapshot:
    matching_item = next((workflow_item for workflow_item in workflow_items if workflow_item.id == item.item_id), None)
    workflow_fields = matching_item.model_dump(include=set(WorkflowFields.model_fields)) if matching_item else {}
    return WorkflowLifecycleVisibilitySnapshot.model_validate(
        {
            **item.model_dump(),
            **workflow_fields,
        }
    )


def _workflow_event_snapshot(event: EventRecord) -> WorkflowEventRecordSnapshot:
    projection = build_event_workflow_projection(event)
    return WorkflowEventRecordSnapshot.model_validate(
        {
            **event.model_dump(),
            **projection.model_dump(),
        }
    )


def _configured_url(value: str | None) -> bool:
    return bool(value and value.rstrip("/") not in {"https://example.com", "http://example.com"})
