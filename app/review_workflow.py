from pydantic import BaseModel

from app.github_events import GitHubEventType, ParsedGitHubEvent
from app.task_state import TaskState, transition_task_state

AGENT_INTEGRATION_REF = "refs/heads/agent-integration"
AGENT_INTEGRATION_BRANCH = "agent-integration"
STATUS_DONE_MARKER = "status: done"
REVIEW_NEXT_ACTION = "Build review prompt and prepare BB/Jarvis Architect review stub."
REQUEUE_NEXT_ACTION = "Post Slack task packet so Circuit can continue from GitHub comment feedback."
NO_REVIEW_NEXT_ACTION = "No review action needed for this event."

REQUEUE_COMMENT_ACTIONS = {"created", "edited"}
REQUEUE_KEYWORD_STATES = {
    "@circuit-forge": TaskState.NEEDS_CHANGES,
    "needs_changes": TaskState.NEEDS_CHANGES,
    "approved_for_human_review": TaskState.APPROVED_FOR_HUMAN_REVIEW,
    "architecture_blocked": TaskState.BLOCKED,
    "architect_review_required": TaskState.BLOCKED,
    "merge_blocked_needs_bb": TaskState.BLOCKED,
    "bb-review-needed": TaskState.BLOCKED,
}


class ReviewContext(BaseModel):
    repo: str | None = None
    issue_number: int | None = None
    pull_request_number: int | None = None
    commit_sha: str | None = None
    event_type: GitHubEventType
    trigger: str


class RequeueContext(BaseModel):
    repo: str | None = None
    issue_number: int | None = None
    pull_request_number: int | None = None
    labels: list[str]
    url: str | None = None
    comment_text: str
    matched_keyword: str
    trigger: str


class ReviewWorkflowResult(BaseModel):
    event_accepted: bool = True
    task_state: TaskState
    repo: str | None = None
    issue_number: int | None = None
    pull_request_number: int | None = None
    commit_sha: str | None = None
    review_context: ReviewContext | None = None
    requeue_context: RequeueContext | None = None
    next_intended_action: str


def build_review_workflow(parsed: ParsedGitHubEvent) -> ReviewWorkflowResult:
    requeue_context = _build_requeue_context(parsed)
    if requeue_context:
        next_state = _task_state_for_requeue_keyword(requeue_context.matched_keyword)
        return ReviewWorkflowResult(
            task_state=next_state,
            repo=parsed.repository,
            issue_number=parsed.issue_number,
            pull_request_number=parsed.pull_request_number,
            commit_sha=parsed.head_sha,
            requeue_context=requeue_context,
            next_intended_action=REQUEUE_NEXT_ACTION,
        )

    trigger = _review_trigger(parsed)
    next_state = transition_task_state(TaskState.WORKING, "review_needed" if trigger else None)
    context = _build_review_context(parsed, trigger) if trigger else None
    return ReviewWorkflowResult(
        task_state=next_state,
        repo=parsed.repository,
        issue_number=parsed.issue_number,
        pull_request_number=parsed.pull_request_number,
        commit_sha=parsed.head_sha,
        review_context=context,
        next_intended_action=REVIEW_NEXT_ACTION if context else NO_REVIEW_NEXT_ACTION,
    )


def _review_trigger(parsed: ParsedGitHubEvent) -> str | None:
    if parsed.event_type == GitHubEventType.PUSH and parsed.ref == AGENT_INTEGRATION_REF:
        return "push_agent_integration"

    if parsed.event_type == GitHubEventType.ISSUE_COMMENT and _contains_status_done(parsed.comment_body):
        return "issue_comment_status_done"

    if (
        parsed.event_type == GitHubEventType.PULL_REQUEST
        and parsed.action in {"opened", "synchronize"}
        and parsed.head_ref == AGENT_INTEGRATION_BRANCH
    ):
        return "pull_request_agent_integration"

    return None


def _build_review_context(parsed: ParsedGitHubEvent, trigger: str) -> ReviewContext:
    return ReviewContext(
        repo=parsed.repository,
        issue_number=parsed.issue_number,
        pull_request_number=parsed.pull_request_number,
        commit_sha=parsed.head_sha,
        event_type=parsed.event_type,
        trigger=trigger,
    )


def _build_requeue_context(parsed: ParsedGitHubEvent) -> RequeueContext | None:
    if parsed.event_type != GitHubEventType.ISSUE_COMMENT or parsed.action not in REQUEUE_COMMENT_ACTIONS:
        return None

    matched_keyword = _matched_requeue_keyword(parsed.comment_body)
    if not matched_keyword:
        return None

    return RequeueContext(
        repo=parsed.repository,
        issue_number=parsed.issue_number,
        pull_request_number=parsed.pull_request_number,
        labels=parsed.labels,
        url=parsed.html_url,
        comment_text=parsed.comment_body or "",
        matched_keyword=matched_keyword,
        trigger="issue_comment_requeue",
    )


def _matched_requeue_keyword(body: str | None) -> str | None:
    normalized = (body or "").lower()
    for keyword in REQUEUE_KEYWORD_STATES:
        if keyword in normalized:
            return keyword
    return None


def _task_state_for_requeue_keyword(keyword: str) -> TaskState:
    return REQUEUE_KEYWORD_STATES.get(keyword, TaskState.NEEDS_CHANGES)


def _contains_status_done(body: str | None) -> bool:
    return STATUS_DONE_MARKER in (body or "").lower()
