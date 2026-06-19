import asyncio
from typing import Any

from app.task_dispatch import dispatch_next_agent_task, list_agent_ready_issues


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeTaskDispatchClient:
    def __init__(self, issues: list[dict[str, Any]] | None = None, issue_lookup: dict[int, dict[str, Any]] | None = None) -> None:
        self.issues = issues or []
        self.issue_lookup = issue_lookup or {}
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
        return self.issues

    async def fetch_issue(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        return self.issue_lookup[issue_number]

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        self.comments.append((repo_full_name, issue_number, body))
        return {"id": 1}

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.labels.append((repo_full_name, issue_number, label))
        return {"labels": [label]}


def issue(number: int, *, created_at: str, labels: list[str], body: str | None = None) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Task {number}",
        "body": body or "Do the thing.",
        "created_at": created_at,
        "labels": [{"name": label} for label in labels],
    }


def test_list_agent_ready_issues_skips_dependency_blocked_tasks() -> None:
    client = FakeTaskDispatchClient(
        [
            issue(1, created_at="2026-06-01T00:00:00Z", labels=["agent-task", "agent-ready"], body="predecessor_issue: 72"),
            issue(2, created_at="2026-06-02T00:00:00Z", labels=["agent-task", "agent-ready"]),
        ],
        {72: {"state": "open", "labels": []}},
    )

    ready = run(list_agent_ready_issues("riseos/example", client))

    assert [item.number for item in ready] == [2]


def test_list_agent_ready_issues_includes_dependency_satisfied_tasks() -> None:
    client = FakeTaskDispatchClient(
        [issue(1, created_at="2026-06-01T00:00:00Z", labels=["agent-task", "agent-ready"], body="predecessor_issue: 72")],
        {72: {"state": "open", "labels": [{"name": "bb2-approved"}, {"name": "ready-to-merge"}]}},
    )

    ready = run(list_agent_ready_issues("riseos/example", client))

    assert [item.number for item in ready] == [1]
    assert ready[0].dependency_count == 1
    assert ready[0].dependencies_satisfied is True
    assert ready[0].blocked_by == []


def test_dispatch_reports_dependency_state_for_selected_task() -> None:
    client = FakeTaskDispatchClient(
        [issue(1, created_at="2026-06-01T00:00:00Z", labels=["agent-task", "agent-ready"], body="predecessor_issue: 72")],
        {72: {"state": "open", "labels": [{"name": "bb2-approved"}, {"name": "ready-to-merge"}]}},
    )

    result = run(dispatch_next_agent_task("riseos/example", client, enabled=True))

    assert result.success is True
    assert result.issue_number == 1
    assert result.dependency_count == 1
    assert result.dependencies_satisfied is True
    assert result.blocked_by == []
