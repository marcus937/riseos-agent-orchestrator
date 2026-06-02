from pydantic import BaseModel

from app.github_events import GitHubEventType, ParsedGitHubEvent
from app.task_state import TaskState, transition_task_state

AGENT_INTEGRATION_REF = "refs/heads/agent-integration"
STATUS_DONE_MARKER = "status: done"
REVIEW_NEXT_ACTION = "Build review prompt and prepare BB/Jarvis Architect review stub."
NO_REVIEW_NEXT_ACTION = "No review action needed for this event."


class ReviewContext(BaseModel):
    repo: str | None = None
    issue_number: int | None = None
    pull_request_number: int | None = None
    commit_sha: str | None = None
    event_type: GitHubEventType
    trigger: str


class ReviewWorkflowResult(BaseModel):
    event_accepted: bool = True
    task_state: TaskState
    repo: str | None = None
    issue_number: int | None = None
    pull_request_number: int | None = None
    commit_sha: str | None = None
    review_context: ReviewContext | None = None
    next_intended_action: str


def build_review_workflow(parsed: ParsedGitHubEvent) -> ReviewWorkflowResult:
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

    if parsed.event_type == GitHubEventType.PULL_REQUEST and parsed.action in {"opened", "synchronize"}:
        return "pull_request_review_event"

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


def _contains_status_done(body: str | None) -> bool:
    return STATUS_DONE_MARKER in (body or "").lower()
