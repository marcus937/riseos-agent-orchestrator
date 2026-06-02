from app.github_events import parse_github_event
from app.review_workflow import REVIEW_NEXT_ACTION, build_review_workflow
from app.task_state import TaskState, transition_task_state


def test_push_to_agent_integration_requests_review() -> None:
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
            "sender": {"login": "agent"},
        },
    )

    result = build_review_workflow(parsed)

    assert result.event_accepted is True
    assert result.task_state == TaskState.REVIEW_NEEDED
    assert result.repo == "marcus937/riseos-agent-orchestrator"
    assert result.commit_sha == "abc123"
    assert result.review_context is not None
    assert result.review_context.trigger == "push_agent_integration"
    assert result.next_intended_action == REVIEW_NEXT_ACTION


def test_status_done_issue_comment_requests_review() -> None:
    parsed = parse_github_event(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
            "issue": {"number": 42},
            "comment": {"body": "Status: Done\nReady for review."},
            "sender": {"login": "agent"},
        },
    )

    result = build_review_workflow(parsed)

    assert result.task_state == TaskState.REVIEW_NEEDED
    assert result.issue_number == 42
    assert result.review_context is not None
    assert result.review_context.issue_number == 42
    assert result.review_context.trigger == "issue_comment_status_done"


def test_pull_request_opened_from_agent_integration_requests_review() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "number": 7,
            "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
            "pull_request": {
                "number": 7,
                "head": {"ref": "agent-integration", "sha": "def456"},
                "base": {"ref": "main"},
            },
        },
    )

    result = build_review_workflow(parsed)

    assert result.task_state == TaskState.REVIEW_NEEDED
    assert result.pull_request_number == 7
    assert result.commit_sha == "def456"
    assert result.review_context is not None
    assert result.review_context.trigger == "pull_request_agent_integration"


def test_pull_request_synchronize_from_agent_integration_requests_review() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "synchronize",
            "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
            "pull_request": {
                "number": 8,
                "head": {"ref": "agent-integration", "sha": "feedface"},
                "base": {"ref": "main"},
            },
        },
    )

    result = build_review_workflow(parsed)

    assert result.task_state == TaskState.REVIEW_NEEDED
    assert result.pull_request_number == 8
    assert result.commit_sha == "feedface"
    assert result.review_context is not None


def test_non_matching_supported_event_stays_working() -> None:
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
            "ref": "refs/heads/main",
            "after": "abc123",
        },
    )

    result = build_review_workflow(parsed)

    assert result.event_accepted is True
    assert result.task_state == TaskState.WORKING
    assert result.review_context is None
    assert result.next_intended_action == "No review action needed for this event."


def test_task_state_transition_helper_supports_requested_states() -> None:
    assert transition_task_state(TaskState.PENDING, None) == TaskState.PENDING
    assert transition_task_state(TaskState.PENDING, "review_needed") == TaskState.REVIEW_NEEDED
    assert transition_task_state(TaskState.REVIEW_NEEDED, "needs_changes") == TaskState.NEEDS_CHANGES
    assert transition_task_state(TaskState.NEEDS_CHANGES, "approved_for_human_review") == TaskState.APPROVED_FOR_HUMAN_REVIEW
    assert transition_task_state(TaskState.WORKING, "blocked") == TaskState.BLOCKED
    assert transition_task_state(TaskState.WORKING, "done") == TaskState.DONE
