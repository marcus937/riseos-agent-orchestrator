import asyncio
from typing import Any

from app.github_events import parse_github_event
from app.github_writeback import GitHubWritebackClient, build_writeback_comment, writeback_review_decision
from app.review_queue import process_review_work_item, review_work_item_from_parsed


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeWritebackClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str]] = []

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        self.comments.append((repo_full_name, issue_number, body))
        if self.error:
            raise self.error
        return {"id": 1}

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.labels.append((repo_full_name, issue_number, label))
        if self.error:
            raise self.error
        return {"labels": [label]}


def test_writeback_disabled_calls_no_github_writes() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "abc123"}},
        },
    )
    response = process_review_work_item(review_work_item_from_parsed(parsed))
    client = FakeWritebackClient()

    assert response.github_writeback_attempted is False
    assert response.github_writeback_success is False
    assert client.comments == []
    assert client.labels == []


def test_pr_target_posts_comment_and_label() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "abc123"}},
        },
    )
    response = process_review_work_item(
        review_work_item_from_parsed(parsed),
        changed_files=["app/main.py"],
        diff_summary="commit abc123: 1 changed file(s), +4/-1.",
    )
    client = FakeWritebackClient()

    result = run(writeback_review_decision(response, client))

    assert result.attempted is True
    assert result.success is True
    assert client.comments[0][0] == "riseos/example"
    assert client.comments[0][1] == 7
    assert "## Review Decision" in client.comments[0][2]
    assert "Dry-run review processor accepted this work item for human review." in client.comments[0][2]
    assert client.labels == [("riseos/example", 7, "bb2-approved")]


def test_issue_target_posts_comment_and_label() -> None:
    parsed = parse_github_event(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "riseos/example"},
            "issue": {"number": 42},
            "comment": {"body": "Status: Done"},
        },
    )
    item = review_work_item_from_parsed(parsed)
    item.commit_sha = "abc123"
    response = process_review_work_item(item)
    client = FakeWritebackClient()

    result = run(writeback_review_decision(response, client))

    assert result.success is True
    assert client.comments[0][1] == 42
    assert client.labels == [("riseos/example", 42, "bb2-approved")]


def test_missing_issue_or_pr_skips_cleanly() -> None:
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
        },
    )
    response = process_review_work_item(review_work_item_from_parsed(parsed))
    client = FakeWritebackClient()

    result = run(writeback_review_decision(response, client))

    assert result.attempted is False
    assert result.success is False
    assert "issue_number or pr_number" in result.error
    assert client.comments == []
    assert client.labels == []


def test_github_error_is_captured() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "abc123"}},
        },
    )
    response = process_review_work_item(review_work_item_from_parsed(parsed))
    client = FakeWritebackClient(error=RuntimeError("GitHub write failed"))

    result = run(writeback_review_decision(response, client))

    assert result.attempted is True
    assert result.success is False
    assert "GitHub write failed" in result.error
    assert client.comments
    assert client.labels == []


def test_writeback_protocol_has_no_forbidden_mutation_methods() -> None:
    allowed = {"post_issue_comment", "apply_label"}
    forbidden = {"merge", "merge_pull_request", "delete_branch", "create_file", "update_file", "create_release"}

    protocol_methods = {name for name in dir(GitHubWritebackClient) if not name.startswith("_")}

    assert allowed.issubset(protocol_methods)
    assert protocol_methods.isdisjoint(forbidden)


def test_comment_body_contains_required_sections() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "abc123"}},
        },
    )
    response = process_review_work_item(
        review_work_item_from_parsed(parsed),
        changed_files=["app/main.py"],
        diff_summary="commit abc123: 1 changed file(s), +4/-1.",
    )

    body = build_writeback_comment(response)

    for section in [
        "Review Decision",
        "Risk Level",
        "Summary",
        "Required Changes",
        "Changed Files",
        "Diff Summary",
        "Dry-run Status",
        "Human Review Required",
    ]:
        assert section in body
