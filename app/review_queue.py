from collections import Counter, deque
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from app.github_events import GitHubEventType, ParsedGitHubEvent
from app.reviewer.decision import ReviewDecision, ReviewDecisionType, RiskLevel
from app.review_workflow import ReviewWorkflowResult


class ReviewWorkItemStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    REVIEWING = "reviewing"
    NEEDS_CHANGES = "needs_changes"
    APPROVED_FOR_HUMAN_REVIEW = "approved_for_human_review"
    BLOCKED = "blocked"


class ReviewLifecycleStage(StrEnum):
    REVIEW_QUEUED = "review_queued"
    WORKER_CLAIMED = "worker_claimed"
    REVIEW_STARTED = "review_started"
    OPENAI_REVIEW_ATTEMPTED = "openai_review_attempted"
    OPENAI_REVIEW_SUCCEEDED = "openai_review_succeeded"
    OPENAI_REVIEW_FAILED = "openai_review_failed"
    REVIEW_COMPLETED = "review_completed"
    REVIEW_FAILED = "review_failed"
    GITHUB_WRITEBACK_STARTED = "github_writeback_started"
    GITHUB_WRITEBACK_COMPLETED = "github_writeback_completed"


class ReviewWorkItem(BaseModel):
    id: str
    created_at: datetime
    updated_at: datetime | None = None
    repo_full_name: str | None = None
    event_type: GitHubEventType
    branch: str | None = None
    commit_sha: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    labels: list[str] = Field(default_factory=list)
    status: ReviewWorkItemStatus = ReviewWorkItemStatus.PENDING_REVIEW
    lifecycle_stage: ReviewLifecycleStage = ReviewLifecycleStage.REVIEW_QUEUED
    worker_claimed_at: datetime | None = None
    review_started_at: datetime | None = None
    openai_review_attempted_at: datetime | None = None
    openai_review_completed_at: datetime | None = None
    review_completed_at: datetime | None = None
    github_writeback_started_at: datetime | None = None
    github_writeback_completed_at: datetime | None = None
    github_writeback_success: bool | None = None
    failure_count: int = 0
    last_failure_at: datetime | None = None
    last_error: str | None = None


class ReviewProcessResponse(BaseModel):
    work_item: ReviewWorkItem
    decision: ReviewDecision
    intended_next_actions: list[str]
    changed_files: list[str] = Field(default_factory=list)
    diff_summary: str | None = None
    diff_patches: list[dict[str, object]] = Field(default_factory=list)
    patch_truncated: bool = False
    github_context_available: bool = False
    github_context_error: str | None = None
    runtime_evidence_context: list[dict[str, object]] = Field(default_factory=list)
    runtime_evidence_error: str | None = None
    runtime_evidence_truncated: bool = False
    github_writeback_attempted: bool = False
    github_writeback_success: bool = False
    github_writeback_error: str | None = None
    task_dispatch_attempted: bool = False
    task_dispatch_success: bool = False
    task_dispatch_issue_number: int | None = None
    task_dispatch_error: str | None = None
    openai_review_attempted: bool = False
    openai_review_success: bool = False
    openai_review_error: str | None = None
    reviewer_model: str | None = None
    dry_run: bool = True


class ReviewQueueCounters(BaseModel):
    review_queue_count: int
    pending_review_count: int
    reviewing_count: int
    needs_changes_count: int
    approved_count: int
    approved_for_human_review_count: int
    blocked_count: int


class ReviewQueueStats(BaseModel):
    counters: ReviewQueueCounters
    oldest_pending_age_seconds: float | None = None
    newest_item_age_seconds: float | None = None
    failure_count: int
    recent_failure_count: int
    last_failure_at: datetime | None = None


class WorkerStats(BaseModel):
    auto_processing_enabled: bool
    claimed_count: int
    active_reviewing_count: int
    completed_count: int
    failed_count: int
    last_claimed_at: datetime | None = None
    last_review_completed_at: datetime | None = None
    last_failure_at: datetime | None = None


class ReviewLifecycleVisibility(BaseModel):
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
    failure_count: int
    last_failure_at: datetime | None = None
    last_error: str | None = None


class RecentFailure(BaseModel):
    item_id: str
    repo_full_name: str | None = None
    event_type: GitHubEventType
    status: ReviewWorkItemStatus
    lifecycle_stage: ReviewLifecycleStage
    failure_count: int
    last_failure_at: datetime
    last_error: str


class InMemoryReviewQueue:
    def __init__(self) -> None:
        self._items: deque[ReviewWorkItem] = deque()

    def create_from_review_event(
        self,
        parsed: ParsedGitHubEvent,
        workflow: ReviewWorkflowResult,
    ) -> ReviewWorkItem | None:
        if workflow.review_context is None:
            return None

        item = review_work_item_from_parsed(parsed)
        return self.add_if_absent(item)

    def add_if_absent(self, item: ReviewWorkItem) -> ReviewWorkItem:
        duplicate = self.find_pending_duplicate(item)
        if duplicate is not None:
            return duplicate
        self._items.append(item)
        return item

    def find_pending_duplicate(self, item: ReviewWorkItem) -> ReviewWorkItem | None:
        identity = review_work_item_identity(item)
        for existing in self._items:
            if existing.status in _UNFINISHED_STATUSES and review_work_item_identity(existing) == identity:
                return existing
        return None

    def claim_item(self, item_id: str) -> ReviewWorkItem | None:
        item = self.get_item(item_id)
        if item is None or item.status != ReviewWorkItemStatus.PENDING_REVIEW:
            return None
        item.status = ReviewWorkItemStatus.REVIEWING
        record_lifecycle_stage(item, ReviewLifecycleStage.WORKER_CLAIMED)
        return item

    def reset_item_for_retry(self, item_id: str, *, error: str | None = None) -> ReviewWorkItem | None:
        item = self.get_item(item_id)
        if item is None:
            return None
        if item.status == ReviewWorkItemStatus.REVIEWING:
            item.status = ReviewWorkItemStatus.PENDING_REVIEW
        if error:
            record_lifecycle_stage(item, ReviewLifecycleStage.REVIEW_FAILED, error=error)
        return item

    def list_items(self) -> list[ReviewWorkItem]:
        return list(reversed(self._items))

    def get_item(self, item_id: str) -> ReviewWorkItem | None:
        for item in self._items:
            if item.id == item_id:
                return item
        return None

    def process_item(self, item_id: str) -> ReviewProcessResponse | None:
        item = self.get_item(item_id)
        if item is None:
            return None

        return process_review_work_item(item)

    def counters(self) -> ReviewQueueCounters:
        return review_queue_counters(self._items)

    def reset(self) -> None:
        self._items.clear()

    def prune_processed(self, max_items: int) -> int:
        removed = 0
        while len(self._items) > max_items:
            for item in list(self._items):
                if item.status not in _UNFINISHED_STATUSES:
                    self._items.remove(item)
                    removed += 1
                    break
            else:
                break
        return removed


def review_work_item_from_parsed(parsed: ParsedGitHubEvent) -> ReviewWorkItem:
    now = datetime.now(UTC)
    return ReviewWorkItem(
        id=str(uuid4()),
        created_at=now,
        updated_at=now,
        repo_full_name=parsed.repository,
        event_type=parsed.event_type,
        branch=_branch_from_parsed(parsed),
        commit_sha=parsed.head_sha,
        issue_number=parsed.issue_number,
        pr_number=parsed.pull_request_number,
        labels=sorted(set(parsed.labels)),
    )


def review_work_item_identity(item: ReviewWorkItem) -> tuple[str | None, str, str | None, int | None, int | None]:
    return (
        item.repo_full_name,
        str(item.event_type),
        item.commit_sha,
        item.pr_number,
        item.issue_number,
    )


def record_lifecycle_stage(
    item: ReviewWorkItem,
    stage: ReviewLifecycleStage,
    *,
    success: bool | None = None,
    error: str | None = None,
) -> ReviewWorkItem:
    now = datetime.now(UTC)
    item.updated_at = now
    item.lifecycle_stage = stage
    if stage == ReviewLifecycleStage.WORKER_CLAIMED:
        item.worker_claimed_at = now
    elif stage == ReviewLifecycleStage.REVIEW_STARTED:
        item.review_started_at = now
    elif stage == ReviewLifecycleStage.OPENAI_REVIEW_ATTEMPTED:
        item.openai_review_attempted_at = now
    elif stage in {ReviewLifecycleStage.OPENAI_REVIEW_SUCCEEDED, ReviewLifecycleStage.OPENAI_REVIEW_FAILED}:
        item.openai_review_completed_at = now
    elif stage == ReviewLifecycleStage.REVIEW_COMPLETED:
        item.review_completed_at = now
    elif stage == ReviewLifecycleStage.REVIEW_FAILED:
        item.last_failure_at = now
        item.failure_count += 1
    elif stage == ReviewLifecycleStage.GITHUB_WRITEBACK_STARTED:
        item.github_writeback_started_at = now
    elif stage == ReviewLifecycleStage.GITHUB_WRITEBACK_COMPLETED:
        item.github_writeback_completed_at = now
        item.github_writeback_success = success
    if error:
        item.last_error = error
        item.last_failure_at = now
        if stage != ReviewLifecycleStage.REVIEW_FAILED:
            item.failure_count += 1
    return item


def review_queue_counters(items: list[ReviewWorkItem] | deque[ReviewWorkItem]) -> ReviewQueueCounters:
    status_counts = Counter(item.status for item in items)
    return ReviewQueueCounters(
        review_queue_count=len(items),
        pending_review_count=status_counts[ReviewWorkItemStatus.PENDING_REVIEW],
        reviewing_count=status_counts[ReviewWorkItemStatus.REVIEWING],
        needs_changes_count=status_counts[ReviewWorkItemStatus.NEEDS_CHANGES],
        approved_count=status_counts[ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW],
        approved_for_human_review_count=status_counts[ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW],
        blocked_count=status_counts[ReviewWorkItemStatus.BLOCKED],
    )


def build_queue_stats(items: list[ReviewWorkItem], counters: ReviewQueueCounters | None = None) -> ReviewQueueStats:
    now = datetime.now(UTC)
    pending_items = [item for item in items if item.status == ReviewWorkItemStatus.PENDING_REVIEW]
    failed_items = [item for item in items if item.last_failure_at is not None]
    return ReviewQueueStats(
        counters=counters or review_queue_counters(items),
        oldest_pending_age_seconds=_oldest_age_seconds(pending_items, now),
        newest_item_age_seconds=_newest_age_seconds(items, now),
        failure_count=sum(item.failure_count for item in items),
        recent_failure_count=len(failed_items),
        last_failure_at=max((item.last_failure_at for item in failed_items if item.last_failure_at), default=None),
    )


def build_worker_stats(items: list[ReviewWorkItem], *, auto_processing_enabled: bool) -> WorkerStats:
    return WorkerStats(
        auto_processing_enabled=auto_processing_enabled,
        claimed_count=sum(1 for item in items if item.worker_claimed_at is not None),
        active_reviewing_count=sum(1 for item in items if item.status == ReviewWorkItemStatus.REVIEWING),
        completed_count=sum(1 for item in items if item.review_completed_at is not None),
        failed_count=sum(item.failure_count for item in items),
        last_claimed_at=max((item.worker_claimed_at for item in items if item.worker_claimed_at), default=None),
        last_review_completed_at=max((item.review_completed_at for item in items if item.review_completed_at), default=None),
        last_failure_at=max((item.last_failure_at for item in items if item.last_failure_at), default=None),
    )


def build_lifecycle_visibility(items: list[ReviewWorkItem]) -> list[ReviewLifecycleVisibility]:
    return [
        ReviewLifecycleVisibility(
            item_id=item.id,
            repo_full_name=item.repo_full_name,
            event_type=item.event_type,
            status=item.status,
            lifecycle_stage=item.lifecycle_stage,
            queued_at=item.created_at,
            worker_claimed_at=item.worker_claimed_at,
            review_started_at=item.review_started_at,
            openai_review_attempted_at=item.openai_review_attempted_at,
            openai_review_completed_at=item.openai_review_completed_at,
            review_completed_at=item.review_completed_at,
            github_writeback_started_at=item.github_writeback_started_at,
            github_writeback_completed_at=item.github_writeback_completed_at,
            github_writeback_success=item.github_writeback_success,
            failure_count=item.failure_count,
            last_failure_at=item.last_failure_at,
            last_error=item.last_error,
        )
        for item in items
    ]


def build_recent_failures(items: list[ReviewWorkItem], *, limit: int = 20) -> list[RecentFailure]:
    failures = [
        RecentFailure(
            item_id=item.id,
            repo_full_name=item.repo_full_name,
            event_type=item.event_type,
            status=item.status,
            lifecycle_stage=item.lifecycle_stage,
            failure_count=item.failure_count,
            last_failure_at=item.last_failure_at,
            last_error=item.last_error or "Unknown review failure.",
        )
        for item in items
        if item.last_failure_at is not None and item.last_error
    ]
    return sorted(failures, key=lambda failure: failure.last_failure_at, reverse=True)[:limit]


def _branch_from_parsed(parsed: ParsedGitHubEvent) -> str | None:
    if parsed.head_ref:
        return parsed.head_ref
    if parsed.ref and parsed.ref.startswith("refs/heads/"):
        return parsed.ref.removeprefix("refs/heads/")
    return parsed.ref


def build_dry_run_review_decision(item: ReviewWorkItem) -> ReviewDecision:
    blocked_reason = _blocked_reason(item)
    if blocked_reason:
        return ReviewDecision(
            decision=ReviewDecisionType.BLOCKED,
            confidence=1.0,
            risk_level=RiskLevel.HIGH,
            summary=blocked_reason,
            required_changes=[blocked_reason],
            next_task_prompt="Provide the missing review context and retry dry-run processing.",
            human_review_required=True,
        )

    return ReviewDecision(
        decision=ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
        confidence=1.0,
        risk_level=RiskLevel.LOW,
        summary="Dry-run review processor accepted this work item for human review.",
        required_changes=[],
        next_task_prompt=None,
        human_review_required=True,
    )


def process_review_work_item(
    item: ReviewWorkItem,
    *,
    decision: ReviewDecision | None = None,
    changed_files: list[str] | None = None,
    diff_summary: str | None = None,
    diff_patches: list[dict[str, object]] | None = None,
    patch_truncated: bool = False,
    github_context_available: bool = False,
    github_context_error: str | None = None,
    runtime_evidence_context: list[dict[str, object]] | None = None,
    runtime_evidence_error: str | None = None,
    runtime_evidence_truncated: bool = False,
    github_writeback_attempted: bool = False,
    github_writeback_success: bool = False,
    github_writeback_error: str | None = None,
    openai_review_attempted: bool = False,
    openai_review_success: bool = False,
    openai_review_error: str | None = None,
    reviewer_model: str | None = None,
) -> ReviewProcessResponse:
    if item.status == ReviewWorkItemStatus.PENDING_REVIEW:
        item.status = ReviewWorkItemStatus.REVIEWING
        record_lifecycle_stage(item, ReviewLifecycleStage.REVIEW_STARTED)

    decision = decision or build_dry_run_review_decision(item)
    item.status = _status_from_decision(decision)
    if github_context_error:
        item.last_error = github_context_error
    if github_writeback_error:
        record_lifecycle_stage(item, ReviewLifecycleStage.GITHUB_WRITEBACK_COMPLETED, success=False, error=github_writeback_error)
    return ReviewProcessResponse(
        work_item=item,
        decision=decision,
        intended_next_actions=_intended_next_actions(decision),
        changed_files=changed_files or [],
        diff_summary=diff_summary,
        diff_patches=diff_patches or [],
        patch_truncated=patch_truncated,
        github_context_available=github_context_available,
        github_context_error=github_context_error,
        runtime_evidence_context=runtime_evidence_context or [],
        runtime_evidence_error=runtime_evidence_error,
        runtime_evidence_truncated=runtime_evidence_truncated,
        github_writeback_attempted=github_writeback_attempted,
        github_writeback_success=github_writeback_success,
        github_writeback_error=github_writeback_error,
        openai_review_attempted=openai_review_attempted,
        openai_review_success=openai_review_success,
        openai_review_error=openai_review_error,
        reviewer_model=reviewer_model,
        dry_run=True,
    )


def _blocked_reason(item: ReviewWorkItem) -> str | None:
    if not item.repo_full_name:
        return "Review work item is missing repo_full_name."
    if not item.commit_sha and item.pr_number is None:
        return "Review work item is missing both commit_sha and pr_number."
    if item.event_type not in set(GitHubEventType):
        return f"Review work item event_type is unsupported: {item.event_type}."
    return None


def _status_from_decision(decision: ReviewDecision) -> ReviewWorkItemStatus:
    if decision.decision in {ReviewDecisionType.BLOCKED, ReviewDecisionType.ESCALATE_TO_MARCUS}:
        return ReviewWorkItemStatus.BLOCKED
    if decision.decision == ReviewDecisionType.NEEDS_CHANGES:
        return ReviewWorkItemStatus.NEEDS_CHANGES
    return ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW


def _intended_next_actions(decision: ReviewDecision) -> list[str]:
    if decision.decision in {ReviewDecisionType.BLOCKED, ReviewDecisionType.ESCALATE_TO_MARCUS}:
        return ["Keep the item blocked until required context is supplied."]
    if decision.decision == ReviewDecisionType.NEEDS_CHANGES:
        return ["Prepare a follow-up task prompt for the coding agent.", "Wait for human approval before any merge."]
    return ["Send the dry-run decision to BB/Jarvis Architect for human review.", "Do not merge automatically."]


def _oldest_age_seconds(items: list[ReviewWorkItem], now: datetime) -> float | None:
    if not items:
        return None
    return round((now - min(item.created_at for item in items)).total_seconds(), 3)


def _newest_age_seconds(items: list[ReviewWorkItem], now: datetime) -> float | None:
    if not items:
        return None
    return round((now - max(item.created_at for item in items)).total_seconds(), 3)


_UNFINISHED_STATUSES = {ReviewWorkItemStatus.PENDING_REVIEW, ReviewWorkItemStatus.REVIEWING}

review_queue = InMemoryReviewQueue()
