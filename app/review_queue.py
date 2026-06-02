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


class ReviewWorkItem(BaseModel):
    id: str
    created_at: datetime
    repo_full_name: str | None = None
    event_type: GitHubEventType
    branch: str | None = None
    commit_sha: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    status: ReviewWorkItemStatus = ReviewWorkItemStatus.PENDING_REVIEW


class ReviewProcessResponse(BaseModel):
    work_item: ReviewWorkItem
    decision: ReviewDecision
    intended_next_actions: list[str]
    changed_files: list[str] = Field(default_factory=list)
    diff_summary: str | None = None
    github_context_available: bool = False
    github_context_error: str | None = None
    dry_run: bool = True


class ReviewQueueCounters(BaseModel):
    review_queue_count: int
    pending_review_count: int
    reviewing_count: int
    needs_changes_count: int
    approved_count: int
    approved_for_human_review_count: int
    blocked_count: int


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
        self._items.append(item)
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
        status_counts = Counter(item.status for item in self._items)
        return ReviewQueueCounters(
            review_queue_count=len(self._items),
            pending_review_count=status_counts[ReviewWorkItemStatus.PENDING_REVIEW],
            reviewing_count=status_counts[ReviewWorkItemStatus.REVIEWING],
            needs_changes_count=status_counts[ReviewWorkItemStatus.NEEDS_CHANGES],
            approved_count=status_counts[ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW],
            approved_for_human_review_count=status_counts[ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW],
            blocked_count=status_counts[ReviewWorkItemStatus.BLOCKED],
        )

    def reset(self) -> None:
        self._items.clear()


def review_work_item_from_parsed(parsed: ParsedGitHubEvent) -> ReviewWorkItem:
    return ReviewWorkItem(
        id=str(uuid4()),
        created_at=datetime.now(UTC),
        repo_full_name=parsed.repository,
        event_type=parsed.event_type,
        branch=_branch_from_parsed(parsed),
        commit_sha=parsed.head_sha,
        issue_number=parsed.issue_number,
        pr_number=parsed.pull_request_number,
    )


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
    changed_files: list[str] | None = None,
    diff_summary: str | None = None,
    github_context_available: bool = False,
    github_context_error: str | None = None,
) -> ReviewProcessResponse:
    if item.status == ReviewWorkItemStatus.PENDING_REVIEW:
        item.status = ReviewWorkItemStatus.REVIEWING

    decision = build_dry_run_review_decision(item)
    item.status = _status_from_decision(decision)
    return ReviewProcessResponse(
        work_item=item,
        decision=decision,
        intended_next_actions=_intended_next_actions(decision),
        changed_files=changed_files or [],
        diff_summary=diff_summary,
        github_context_available=github_context_available,
        github_context_error=github_context_error,
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
    if decision.decision == ReviewDecisionType.BLOCKED:
        return ReviewWorkItemStatus.BLOCKED
    if decision.decision == ReviewDecisionType.NEEDS_CHANGES:
        return ReviewWorkItemStatus.NEEDS_CHANGES
    return ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW


def _intended_next_actions(decision: ReviewDecision) -> list[str]:
    if decision.decision == ReviewDecisionType.BLOCKED:
        return ["Keep the item blocked until required context is supplied."]
    if decision.decision == ReviewDecisionType.NEEDS_CHANGES:
        return ["Prepare a follow-up task prompt for the coding agent.", "Wait for human approval before any merge."]
    return ["Send the dry-run decision to BB/Jarvis Architect for human review.", "Do not merge automatically."]


review_queue = InMemoryReviewQueue()
