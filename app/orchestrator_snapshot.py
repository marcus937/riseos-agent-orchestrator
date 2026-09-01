from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.config import Settings
from app.event_store import DebugHealth, EventRecord
from app.github_events import GitHubEventType
from app.review_queue import (
    RecentFailure,
    ReviewLifecycleStage,
    ReviewLifecycleVisibility,
    ReviewQueueStats,
    ReviewWorkItem,
    ReviewWorkItemStatus,
    WorkerStats,
)
from app.workflow_lifecycle import (
    LegacyWorkflowState,
    WorkflowOwner,
    WorkflowState,
    WorkflowStateProjection,
    build_event_workflow_projection,
    build_work_item_workflow_projection,
)
from app.workflows import WorkflowSummaryCounts, build_workflow_summaries, build_workflow_summary_counts

ORCHESTRATOR_SNAPSHOT_SCHEMA_VERSION = "orchestrator.snapshot.v2"
ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT = 50
ORCHESTRATOR_SNAPSHOT_EVENT_LIMIT = 25
ORCHESTRATOR_SNAPSHOT_RECENT_FAILURE_LIMIT = 20
ORCHESTRATOR_SNAPSHOT_WORKFLOW_EVENT_LIMIT = 0
ORCHESTRATOR_SNAPSHOT_LABEL_LIMIT = 20
ORCHESTRATOR_SNAPSHOT_TEXT_LIMIT = 2048
_TRUNCATED_SUFFIX = "... [truncated]"


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
    workflow_id: str | None = None
    workflow_state: LegacyWorkflowState | None = None
    canonical_workflow_state: WorkflowState | None = None
    workflow_duration_seconds: float | None = None
    current_owner: WorkflowOwner = WorkflowOwner.UNKNOWN
    workflow_event_count: int = 0
    workflow_events_truncated: bool = False


class WorkflowWorkItemSnapshot(WorkflowFields):
    id: str
    created_at: datetime
    updated_at: datetime | None = None
    repo_full_name: str | None = None
    event_type: GitHubEventType
    branch: str | None = None
    base_branch: str | None = None
    commit_sha: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    labels: list[str] = Field(default_factory=list)
    label_count: int = 0
    labels_truncated: bool = False
    status: ReviewWorkItemStatus
    lifecycle_stage: ReviewLifecycleStage
    worker_claimed_at: datetime | None = None
    review_started_at: datetime | None = None
    openai_review_attempted_at: datetime | None = None
    openai_review_completed_at: datetime | None = None
    review_completed_at: datetime | None = None
    github_writeback_started_at: datetime | None = None
    github_writeback_completed_at: datetime | None = None
    github_writeback_success: bool | None = None
    agent_bus_dispatch_started_at: datetime | None = None
    agent_bus_dispatch_completed_at: datetime | None = None
    agent_bus_dispatch_success: bool | None = None
    agent_bus_work_item_id: str | None = None
    agent_bus_dispatch_error: str | None = None
    agent_bus_dispatch_error_truncated: bool = False
    runtime_validation_id: str | None = None
    runtime_validation_status: str | None = None
    runtime_validation_digest: str | None = None
    runtime_validation_completed_at: datetime | None = None
    failure_count: int = 0
    last_failure_at: datetime | None = None
    last_error: str | None = None
    last_error_truncated: bool = False


class WorkflowLifecycleVisibilitySnapshot(WorkflowFields):
    item_id: str
    repo_full_name: str | None = None
    event_type: GitHubEventType
    status: ReviewWorkItemStatus
    lifecycle_stage: ReviewLifecycleStage
    queued_at: datetime
    worker_claimed_at: datetime | None = None
    review_started_at: datetime | None = None
    openai_review_attempted_at: datetime | None = None
    openai_review_completed_at: datetime | None = None
    review_completed_at: datetime | None = None
    github_writeback_started_at: datetime | None = None
    github_writeback_completed_at: datetime | None = None
    github_writeback_success: bool | None = None
    agent_bus_dispatch_started_at: datetime | None = None
    agent_bus_dispatch_completed_at: datetime | None = None
    agent_bus_dispatch_success: bool | None = None
    agent_bus_work_item_id: str | None = None
    agent_bus_dispatch_error: str | None = None
    agent_bus_dispatch_error_truncated: bool = False
    runtime_validation_id: str | None = None
    runtime_validation_status: str | None = None
    runtime_validation_completed_at: datetime | None = None
    failure_count: int
    last_failure_at: datetime | None = None
    last_error: str | None = None
    last_error_truncated: bool = False


class WorkflowEventRecordSnapshot(WorkflowFields):
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
    pr_merged: bool | None = None
    received_at: datetime
    raw_action: str | None = None


class RecentFailureSnapshot(BaseModel):
    item_id: str
    repo_full_name: str | None = None
    event_type: GitHubEventType
    status: ReviewWorkItemStatus
    lifecycle_stage: ReviewLifecycleStage
    failure_count: int
    last_failure_at: datetime
    last_error: str
    last_error_truncated: bool = False


class SnapshotCollectionMeta(BaseModel):
    returned: int
    total: int
    limit: int
    truncated: bool


class OrchestratorWorkforceSnapshotMeta(BaseModel):
    agents: SnapshotCollectionMeta
    issues: SnapshotCollectionMeta
    prs: SnapshotCollectionMeta
    events: SnapshotCollectionMeta


class OrchestratorWorkforceSnapshot(BaseModel):
    overview: OrchestratorSnapshotOverview
    meta: OrchestratorWorkforceSnapshotMeta
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
    workflows: WorkflowSummaryCounts
    queue: ReviewQueueStats
    health: DebugHealth
    runtime: OrchestratorRuntime
    recent_failures: list[RecentFailureSnapshot] = Field(default_factory=list)


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
    issue_items = [item for item in review_items if item.issue_number is not None and item.pr_number is None]
    pr_items = [item for item in review_items if item.pr_number is not None]
    limited_lifecycle = _limit_collection(lifecycle, ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT)
    limited_issue_items = _limit_collection(issue_items, ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT)
    limited_pr_items = _limit_collection(pr_items, ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT)
    review_items_by_id = {item.id: item for item in review_items}
    workflow_item_ids = {
        *(item.item_id for item in limited_lifecycle),
        *(item.id for item in limited_issue_items),
        *(item.id for item in limited_pr_items),
    }
    workflow_items_by_id = {
        item_id: _workflow_work_item_snapshot(review_items_by_id[item_id])
        for item_id in workflow_item_ids
        if item_id in review_items_by_id
    }
    agents = [_workflow_lifecycle_snapshot(item, workflow_items_by_id) for item in limited_lifecycle]
    issues = [workflow_items_by_id[item.id] for item in limited_issue_items if item.id in workflow_items_by_id]
    prs = [workflow_items_by_id[item.id] for item in limited_pr_items if item.id in workflow_items_by_id]
    event_snapshots = [
        _workflow_event_snapshot(event)
        for event in _limit_collection(events, ORCHESTRATOR_SNAPSHOT_EVENT_LIMIT)
    ]
    workflows = build_workflow_summaries(review_items, events)
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
            meta=OrchestratorWorkforceSnapshotMeta(
                agents=_collection_meta_from_total(len(lifecycle), ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT),
                issues=_collection_meta_from_total(len(issue_items), ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT),
                prs=_collection_meta_from_total(len(pr_items), ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT),
                events=_collection_meta_from_total(len(events), ORCHESTRATOR_SNAPSHOT_EVENT_LIMIT),
            ),
            agents=agents,
            issues=issues,
            prs=prs,
            events=event_snapshots,
        ),
        workflows=build_workflow_summary_counts(workflows),
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
        recent_failures=[
            _recent_failure_snapshot(failure)
            for failure in _limit_collection(recent_failures, ORCHESTRATOR_SNAPSHOT_RECENT_FAILURE_LIMIT)
        ],
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
    labels = _limit_collection(item.labels, ORCHESTRATOR_SNAPSHOT_LABEL_LIMIT)
    last_error, last_error_truncated = _bounded_string(item.last_error)
    agent_bus_dispatch_error, agent_bus_dispatch_error_truncated = _bounded_string(item.agent_bus_dispatch_error)
    return WorkflowWorkItemSnapshot.model_validate(
        {
            "id": item.id,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "repo_full_name": item.repo_full_name,
            "event_type": item.event_type,
            "branch": item.branch,
            "base_branch": item.base_branch,
            "commit_sha": item.commit_sha,
            "issue_number": item.issue_number,
            "pr_number": item.pr_number,
            "labels": labels,
            "label_count": len(item.labels),
            "labels_truncated": len(item.labels) > ORCHESTRATOR_SNAPSHOT_LABEL_LIMIT,
            "status": item.status,
            "lifecycle_stage": item.lifecycle_stage,
            "worker_claimed_at": item.worker_claimed_at,
            "review_started_at": item.review_started_at,
            "openai_review_attempted_at": item.openai_review_attempted_at,
            "openai_review_completed_at": item.openai_review_completed_at,
            "review_completed_at": item.review_completed_at,
            "github_writeback_started_at": item.github_writeback_started_at,
            "github_writeback_completed_at": item.github_writeback_completed_at,
            "github_writeback_success": item.github_writeback_success,
            "agent_bus_dispatch_started_at": item.agent_bus_dispatch_started_at,
            "agent_bus_dispatch_completed_at": item.agent_bus_dispatch_completed_at,
            "agent_bus_dispatch_success": item.agent_bus_dispatch_success,
            "agent_bus_work_item_id": item.agent_bus_work_item_id,
            "agent_bus_dispatch_error": agent_bus_dispatch_error,
            "agent_bus_dispatch_error_truncated": agent_bus_dispatch_error_truncated,
            "runtime_validation_id": item.runtime_validation_id,
            "runtime_validation_status": item.runtime_validation_status,
            "runtime_validation_digest": item.runtime_validation_digest,
            "runtime_validation_completed_at": item.runtime_validation_completed_at,
            "failure_count": item.failure_count,
            "last_failure_at": item.last_failure_at,
            "last_error": last_error,
            "last_error_truncated": last_error_truncated,
            **_workflow_fields(projection, workflow_id=f"wf-{item.id}"),
        }
    )


def _workflow_lifecycle_snapshot(
    item: ReviewLifecycleVisibility,
    workflow_items: dict[str, WorkflowWorkItemSnapshot],
) -> WorkflowLifecycleVisibilitySnapshot:
    matching_item = workflow_items.get(item.item_id)
    workflow_fields = matching_item.model_dump(include=set(WorkflowFields.model_fields)) if matching_item else {}
    last_error, last_error_truncated = _bounded_string(item.last_error)
    agent_bus_dispatch_error, agent_bus_dispatch_error_truncated = _bounded_string(item.agent_bus_dispatch_error)
    return WorkflowLifecycleVisibilitySnapshot.model_validate(
        {
            "item_id": item.item_id,
            "repo_full_name": item.repo_full_name,
            "event_type": item.event_type,
            "status": item.status,
            "lifecycle_stage": item.lifecycle_stage,
            "queued_at": item.queued_at,
            "worker_claimed_at": item.worker_claimed_at,
            "review_started_at": item.review_started_at,
            "openai_review_attempted_at": item.openai_review_attempted_at,
            "openai_review_completed_at": item.openai_review_completed_at,
            "review_completed_at": item.review_completed_at,
            "github_writeback_started_at": item.github_writeback_started_at,
            "github_writeback_completed_at": item.github_writeback_completed_at,
            "github_writeback_success": item.github_writeback_success,
            "agent_bus_dispatch_started_at": item.agent_bus_dispatch_started_at,
            "agent_bus_dispatch_completed_at": item.agent_bus_dispatch_completed_at,
            "agent_bus_dispatch_success": item.agent_bus_dispatch_success,
            "agent_bus_work_item_id": item.agent_bus_work_item_id,
            "agent_bus_dispatch_error": agent_bus_dispatch_error,
            "agent_bus_dispatch_error_truncated": agent_bus_dispatch_error_truncated,
            "runtime_validation_id": item.runtime_validation_id,
            "runtime_validation_status": item.runtime_validation_status,
            "runtime_validation_completed_at": item.runtime_validation_completed_at,
            "failure_count": item.failure_count,
            "last_failure_at": item.last_failure_at,
            "last_error": last_error,
            "last_error_truncated": last_error_truncated,
            "workflow_id": f"wf-{item.item_id}",
            **workflow_fields,
        }
    )


def _workflow_event_snapshot(event: EventRecord) -> WorkflowEventRecordSnapshot:
    projection = build_event_workflow_projection(event)
    return WorkflowEventRecordSnapshot.model_validate(
        {
            "event_id": event.event_id,
            "github_event": event.github_event,
            "diagnostic_stage": event.diagnostic_stage,
            "correlation_id": event.correlation_id,
            "correlation_key": event.correlation_key,
            "repo_full_name": event.repo_full_name,
            "branch": event.branch,
            "commit_sha": event.commit_sha,
            "issue_number": event.issue_number,
            "pr_number": event.pr_number,
            "pr_merged": event.pr_merged,
            "received_at": event.received_at,
            "raw_action": event.raw_action,
            **_workflow_fields(projection, workflow_id=f"wf-{event.correlation_id or event.event_id}"),
        }
    )


def _workflow_fields(projection: WorkflowStateProjection, *, workflow_id: str | None) -> dict[str, Any]:
    workflow_event_count = len(projection.workflow_events)
    return {
        "workflow_id": workflow_id,
        "workflow_state": projection.workflow_state,
        "canonical_workflow_state": projection.canonical_workflow_state,
        "workflow_duration_seconds": projection.workflow_duration_seconds,
        "current_owner": projection.current_owner,
        "workflow_event_count": workflow_event_count,
        "workflow_events_truncated": workflow_event_count > ORCHESTRATOR_SNAPSHOT_WORKFLOW_EVENT_LIMIT,
    }


def _recent_failure_snapshot(failure: RecentFailure) -> RecentFailureSnapshot:
    last_error, last_error_truncated = _bounded_string(failure.last_error)
    return RecentFailureSnapshot(
        item_id=failure.item_id,
        repo_full_name=failure.repo_full_name,
        event_type=failure.event_type,
        status=failure.status,
        lifecycle_stage=failure.lifecycle_stage,
        failure_count=failure.failure_count,
        last_failure_at=failure.last_failure_at,
        last_error=last_error or "Unknown review failure.",
        last_error_truncated=last_error_truncated,
    )


def _collection_meta(items: list[Any], limit: int) -> SnapshotCollectionMeta:
    return _collection_meta_from_total(len(items), limit)


def _collection_meta_from_total(total: int, limit: int) -> SnapshotCollectionMeta:
    return SnapshotCollectionMeta(
        returned=min(total, limit),
        total=total,
        limit=limit,
        truncated=total > limit,
    )


def _limit_collection(items: list[Any], limit: int) -> list[Any]:
    return items[:limit]


def _bounded_string(value: str | None, *, limit: int = ORCHESTRATOR_SNAPSHOT_TEXT_LIMIT) -> tuple[str | None, bool]:
    if value is None or len(value) <= limit:
        return value, False
    if limit <= len(_TRUNCATED_SUFFIX):
        return _TRUNCATED_SUFFIX[:limit], True
    return f"{value[: limit - len(_TRUNCATED_SUFFIX)]}{_TRUNCATED_SUFFIX}", True


def _configured_url(value: str | None) -> bool:
    return bool(value and value.rstrip("/") not in {"https://example.com", "http://example.com"})
