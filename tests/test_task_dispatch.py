import asyncio
from typing import Any

from app.clients.github import GitHubClient
from app.reviewer.decision import ReviewDecisionType
from app.task_dispatch import (
    LABEL_AGENT_NEXT,
    AgentTaskIssue,
    build_circuit_assignment_body,
    dispatch_next_agent_task,
    list_agent_ready_issues,
    select_next_agent_task,
    should_dispatch_next_task,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeTaskDispatchClient:
    def __init__(self, issues: list[dict[str, Any]] | None = None) -> None:
        self.issues = issues or []
        self.list_calls: list[tuple[str, list[str] | None, str, str]] = []
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str]] = []

    async def list_open_issues(
        self,
        repo_full_name: str,
        *,
        labels: list[str] | None = None,
        sort: str = "created",
        direction: str = "asc",
    ) -> list[dict[str, Any]]:
        self.list_calls.append((repo_full_name, labels, sort, direction))
        return self.issues

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        self.comments.append((repo_full_name, issue_number, body))
        return {"id": 1}

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.labels.append((repo_full_name, issue_number, label))
        return {"labels": [label]}


def issue(number: int, *, created_at: str, labels: list[str], title: str | None = None, body: str | None = None) -> dict[str, Any]:
    return {
        "number": number,
        "title": title or f"Task {number}",
        "body": body or "Do the thing.",
        "created_at": created_at,
        "labels": [{"name": label} for label in labels],
    }


def test_dispatch_disabled_does_nothing() -> None:
    client = FakeTaskDispatchClient([issue(1, created_at="2026-06-01T00:00:00Z", labels=["agent-task", "agent-ready"])])

    result = run(dispatch_next_agent_task("riseos/example", client, enabled=False))

    assert result.attempted is False
    assert result.success is False
    assert client.list_calls == []
    assert client.comments == []
    assert client.labels == []


def test_approved_review_selects_oldest_agent_ready_issue() -> None:
    client = FakeTaskDispatchClient(
        [
            issue(2, created_at="2026-06-02T00:00:00Z", labels=["agent-task", "agent-ready"]),
            issue(1, created_at="2026-06-01T00:00:00Z", labels=["agent-task", "agent-ready"]),
            issue(3, created_at="2026-05-01T00:00:00Z", labels=["agent-task", "agent-ready", "bb2-blocked"]),
            {**issue(4, created_at="2026-04-01T00:00:00Z", labels=["agent-task", "agent-ready"]), "pull_request": {}},
        ]
    )

    selected = run(select_next_agent_task("riseos/example", client))

    assert selected is not None
    assert selected.number == 1
    assert client.list_calls[0] == ("riseos/example", ["agent-task", "agent-ready"], "created", "asc")


def test_blocked_review_does_not_dispatch_next_task() -> None:
    assert should_dispatch_next_task(ReviewDecisionType.BLOCKED) is False
    assert should_dispatch_next_task(ReviewDecisionType.ESCALATE_TO_MARCUS) is False


def test_needs_changes_does_not_dispatch_next_task() -> None:
    assert should_dispatch_next_task(ReviewDecisionType.NEEDS_CHANGES) is False


def test_no_ready_issue_is_handled_cleanly() -> None:
    client = FakeTaskDispatchClient([])

    result = run(dispatch_next_agent_task("riseos/example", client, enabled=True))

    assert result.attempted is True
    assert result.success is False
    assert result.issue_number is None
    assert result.error == "No queued agent-ready issue found"
    assert client.comments == []
    assert client.labels == []


def test_dispatch_posts_assignment_comment_and_agent_next_label() -> None:
    client = FakeTaskDispatchClient(
        [issue(8, created_at="2026-06-01T00:00:00Z", labels=["agent-task", "agent-ready"], title="Wire dispatch", body="Implement task dispatch.")]
    )

    result = run(dispatch_next_agent_task("riseos/example", client, enabled=True))

    assert result.attempted is True
    assert result.success is True
    assert result.issue_number == 8
    assert client.comments[0][0:2] == ("riseos/example", 8)
    assert client.labels == [("riseos/example", 8, LABEL_AGENT_NEXT)]
    body = client.comments[0][2]
    assert "Circuit Assignment" in body
    assert "Wire dispatch" in body
    assert "Branch: `agent-integration` only." in body
    assert "Stay on `agent-integration`" in body
    assert "Status: Done" in body
    assert "completed commit SHA" in body
    assert "Do not merge" in body
    assert "Do not open a PR unless explicitly requested" in body
    assert "Implement task dispatch." in body


def test_assignment_comment_body_includes_circuit_instructions() -> None:
    body = build_circuit_assignment_body(
        AgentTaskIssue(
            number=9,
            title="Next queued task",
            body="Task body here.",
            labels=["agent-task", "agent-ready"],
        )
    )

    assert body.startswith("## Circuit Assignment")
    assert "Issue: #9 - Next queued task" in body
    assert "Branch: `agent-integration` only." in body
    assert "Comment `Status: Done` with the completed commit SHA" in body
    assert "Do not merge" in body
    assert "Task body here." in body


def test_list_agent_ready_issues_filters_missing_labels_and_blocked() -> None:
    client = FakeTaskDispatchClient(
        [
            issue(1, created_at="2026-06-01T00:00:00Z", labels=["agent-task", "agent-ready"]),
            issue(2, created_at="2026-06-01T00:00:00Z", labels=["agent-task"]),
            issue(3, created_at="2026-06-01T00:00:00Z", labels=["agent-ready"]),
            issue(4, created_at="2026-06-01T00:00:00Z", labels=["agent-task", "agent-ready", "bb2-blocked"]),
        ]
    )

    ready = run(list_agent_ready_issues("riseos/example", client))

    assert [item.number for item in ready] == [1]


def test_github_client_has_no_forbidden_mutation_methods() -> None:
    forbidden = {
        "merge",
        "merge_pull_request",
        "delete_branch",
        "create_branch",
        "update_ref",
        "close_issue",
        "create_file",
        "update_file",
        "delete_file",
    }
    public_methods = {name for name in dir(GitHubClient) if not name.startswith("_")}

    assert public_methods.isdisjoint(forbidden)
