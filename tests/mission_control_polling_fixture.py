from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.agent_tasks import AgentTask, AgentTaskLifecycleEvent, AgentTaskStatus, SQLiteAgentTaskStore
from app.event_store import EventRecord
from app.github_events import GitHubEventType
from app.review_queue import ReviewLifecycleStage, ReviewWorkItem, ReviewWorkItemStatus
from app.storage import SQLiteStateStore


PRODUCTION_HISTORICAL_WORKFLOW_COUNT = 500
PRODUCTION_AGENT_LIFECYCLE_RECORD_COUNT = 500
PRODUCTION_AGENT_TASK_WORKFLOW_COUNT = 500
PRODUCTION_PR_RECORD_COUNT = 300
PRODUCTION_EVENT_RECORD_COUNT = 50
PRODUCTION_ISSUE_RECORD_COUNT = PRODUCTION_HISTORICAL_WORKFLOW_COUNT - PRODUCTION_PR_RECORD_COUNT
PRODUCTION_TOTAL_WORKFLOW_COUNT = (
    PRODUCTION_HISTORICAL_WORKFLOW_COUNT
    + PRODUCTION_AGENT_TASK_WORKFLOW_COUNT
    + PRODUCTION_EVENT_RECORD_COUNT
)

PRODUCTION_DETAIL_SENTINEL = "mission-control-production-detail-payload"
PRODUCTION_SECRET_SENTINEL = "mission-control-production-secret-value"
PRODUCTION_BASE_TIME = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class MissionControlProductionFixture:
    review_item_count: int = PRODUCTION_HISTORICAL_WORKFLOW_COUNT
    agent_lifecycle_record_count: int = PRODUCTION_AGENT_LIFECYCLE_RECORD_COUNT
    agent_task_workflow_count: int = PRODUCTION_AGENT_TASK_WORKFLOW_COUNT
    pr_record_count: int = PRODUCTION_PR_RECORD_COUNT
    issue_record_count: int = PRODUCTION_ISSUE_RECORD_COUNT
    event_record_count: int = PRODUCTION_EVENT_RECORD_COUNT
    total_workflow_count: int = PRODUCTION_TOTAL_WORKFLOW_COUNT
    detail_sentinel: str = PRODUCTION_DETAIL_SENTINEL
    secret_sentinel: str = PRODUCTION_SECRET_SENTINEL


def seed_mission_control_production_fixture(
    storage: SQLiteStateStore,
    agent_store: SQLiteAgentTaskStore,
    *,
    base_time: datetime = PRODUCTION_BASE_TIME,
) -> MissionControlProductionFixture:
    for index in range(PRODUCTION_HISTORICAL_WORKFLOW_COUNT):
        storage.save_review_work_item(_review_work_item(index, base_time))
    for index in range(PRODUCTION_AGENT_TASK_WORKFLOW_COUNT):
        agent_store.save_agent_task(_agent_task(index, base_time))
    for index in range(PRODUCTION_EVENT_RECORD_COUNT):
        storage.save_event_record(_event_record(index, base_time))
    return MissionControlProductionFixture()


def _review_work_item(index: int, base_time: datetime) -> ReviewWorkItem:
    created_at = base_time + timedelta(minutes=index)
    status = _review_status(index)
    lifecycle_stage = _review_lifecycle_stage(status)
    is_pr = index < PRODUCTION_PR_RECORD_COUNT
    review_started_at = created_at + timedelta(minutes=1) if status != ReviewWorkItemStatus.PENDING_REVIEW else None
    review_completed_at = (
        created_at + timedelta(minutes=5)
        if status
        in {
            ReviewWorkItemStatus.NEEDS_CHANGES,
            ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW,
        }
        else None
    )
    last_failure_at = created_at + timedelta(minutes=6) if status == ReviewWorkItemStatus.BLOCKED else None
    return ReviewWorkItem(
        id=f"prod-review-{index:04d}",
        created_at=created_at,
        updated_at=review_completed_at or last_failure_at or review_started_at or created_at,
        repo_full_name="riseos/mission-control-prod",
        event_type=GitHubEventType.PULL_REQUEST if is_pr else GitHubEventType.ISSUES,
        branch=f"codex/prod-review-{index:04d}",
        base_branch="agent-integration",
        commit_sha=f"prodreviewsha{index:04d}",
        issue_number=None if is_pr else 10_000 + index,
        pr_number=index + 1 if is_pr else None,
        labels=[
            "agent-task",
            "agent-ready",
            f"service-{index % 7}",
            f"priority-{index % 3}",
        ],
        status=status,
        lifecycle_stage=lifecycle_stage,
        worker_claimed_at=created_at + timedelta(seconds=30)
        if status != ReviewWorkItemStatus.PENDING_REVIEW
        else None,
        review_started_at=review_started_at,
        review_completed_at=review_completed_at,
        runtime_validation_id=f"rv-prod-{index:04d}" if is_pr else None,
        runtime_validation_status="passed" if is_pr and index % 2 == 0 else None,
        runtime_validation_digest=f"digest-{index:04d}" if is_pr else None,
        runtime_validation_completed_at=created_at + timedelta(minutes=4) if is_pr and index % 2 == 0 else None,
        runtime_validation_context={
            "detail": f"{PRODUCTION_DETAIL_SENTINEL}-{index}-" + ("x" * 512),
            "secret": f"{PRODUCTION_SECRET_SENTINEL}-{index}",
            "review_dispatch": {
                "prompt": f"{PRODUCTION_DETAIL_SENTINEL}-prompt-{index}",
                "review_packet": {"raw": f"{PRODUCTION_DETAIL_SENTINEL}-packet-{index}"},
            },
        },
        failure_count=1 if status == ReviewWorkItemStatus.BLOCKED else 0,
        last_failure_at=last_failure_at,
        last_error=f"production-shaped review failure {index}" if status == ReviewWorkItemStatus.BLOCKED else None,
    )


def _review_status(index: int) -> ReviewWorkItemStatus:
    statuses = (
        ReviewWorkItemStatus.PENDING_REVIEW,
        ReviewWorkItemStatus.REVIEWING,
        ReviewWorkItemStatus.NEEDS_CHANGES,
        ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW,
        ReviewWorkItemStatus.BLOCKED,
    )
    return statuses[index % len(statuses)]


def _review_lifecycle_stage(status: ReviewWorkItemStatus) -> ReviewLifecycleStage:
    if status == ReviewWorkItemStatus.PENDING_REVIEW:
        return ReviewLifecycleStage.REVIEW_QUEUED
    if status == ReviewWorkItemStatus.REVIEWING:
        return ReviewLifecycleStage.REVIEW_STARTED
    if status == ReviewWorkItemStatus.BLOCKED:
        return ReviewLifecycleStage.REVIEW_FAILED
    return ReviewLifecycleStage.REVIEW_COMPLETED


def _agent_task(index: int, base_time: datetime) -> AgentTask:
    created_at = base_time + timedelta(minutes=1_000 + index)
    status = _agent_task_status(index)
    lifecycle_events = [
        AgentTaskLifecycleEvent(event="created", occurred_at=created_at),
        AgentTaskLifecycleEvent(
            event="queued",
            occurred_at=created_at + timedelta(seconds=5),
            metadata={"target_agent": "codex-m2", "priority": "normal"},
        ),
        AgentTaskLifecycleEvent(
            event=status.value,
            occurred_at=created_at + timedelta(minutes=2),
            actor="codex-m2" if status != AgentTaskStatus.READY_FOR_REVIEW else "bb2",
            metadata={
                "detail": f"{PRODUCTION_DETAIL_SENTINEL}-agent-event-{index}",
                "secret": f"{PRODUCTION_SECRET_SENTINEL}-agent-{index}",
            },
        ),
    ]
    return AgentTask(
        task_id=f"prod-agent-{index:04d}",
        repo_full_name="riseos/mission-control-prod",
        title=f"Production-shaped agent task {index:04d}",
        objective=f"Run production-shaped task {index:04d}.",
        body=f"{PRODUCTION_DETAIL_SENTINEL}-agent-body-{index}-" + ("y" * 512),
        instructions=[f"{PRODUCTION_DETAIL_SENTINEL}-agent-instructions-{index}"],
        acceptance_criteria=[f"Acceptance criterion {index}"],
        target_agent="codex-m2",
        status=status,
        source="direct_api",
        issue_number=20_000 + index,
        agent_bus_work_item_id=f"bus-prod-{index:04d}",
        created_at=created_at,
        updated_at=created_at + timedelta(minutes=2),
        queued_at=created_at + timedelta(seconds=5),
        assigned_at=created_at + timedelta(seconds=30)
        if status
        in {
            AgentTaskStatus.ASSIGNED,
            AgentTaskStatus.RUNNING,
            AgentTaskStatus.READY_FOR_REVIEW,
            AgentTaskStatus.FAILED,
        }
        else None,
        running_at=created_at + timedelta(minutes=1)
        if status in {AgentTaskStatus.RUNNING, AgentTaskStatus.READY_FOR_REVIEW, AgentTaskStatus.FAILED}
        else None,
        failed_at=created_at + timedelta(minutes=2) if status == AgentTaskStatus.FAILED else None,
        branch=f"codex/prod-agent-{index:04d}",
        commit_sha=f"prodagentsha{index:04d}",
        execution_evidence={
            "detail": f"{PRODUCTION_DETAIL_SENTINEL}-agent-evidence-{index}",
            "secret": f"{PRODUCTION_SECRET_SENTINEL}-agent-evidence-{index}",
        },
        lifecycle_events=lifecycle_events,
    )


def _agent_task_status(index: int) -> AgentTaskStatus:
    statuses = (
        AgentTaskStatus.QUEUED,
        AgentTaskStatus.ASSIGNED,
        AgentTaskStatus.RUNNING,
        AgentTaskStatus.READY_FOR_REVIEW,
        AgentTaskStatus.FAILED,
    )
    return statuses[index % len(statuses)]


def _event_record(index: int, base_time: datetime) -> EventRecord:
    received_at = base_time + timedelta(minutes=2_000 + index)
    return EventRecord(
        event_id=f"prod-event-{index:04d}",
        github_event=GitHubEventType.PUSH,
        correlation_id=f"prod-event-workflow-{index:04d}",
        repo_full_name="riseos/mission-control-prod",
        branch=f"codex/prod-event-{index:04d}",
        commit_sha=f"prodeventsha{index:04d}",
        received_at=received_at,
    )
