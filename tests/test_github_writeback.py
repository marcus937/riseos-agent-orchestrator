import asyncio
from typing import Any

from app.github_events import parse_github_event
from app.github_writeback import (
    BB2_STATUS_COMMENT_MARKER,
    GitHubWritebackClient,
    build_writeback_comment,
    writeback_review_decision,
)
from app.reviewer.decision import ReviewDecision, ReviewDecisionType, RiskLevel
from app.review_queue import process_review_work_item, review_work_item_from_parsed


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeWritebackClient:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        fail_on_apply: Exception | None = None,
        fail_on_remove: Exception | None = None,
        fail_on_comment: Exception | None = None,
        initial_labels: list[str] | None = None,
        initial_comments: list[dict[str, Any]] | None = None,
    ) -> None:
        self.error = error
        self.fail_on_apply = fail_on_apply
        self.fail_on_remove = fail_on_remove
        self.fail_on_comment = fail_on_comment
        self.comments: list[tuple[str, int, str]] = []
        self.updated_comments: list[tuple[str, int, str]] = []
        self.applied_labels: list[tuple[str, int, str]] = []
        self.removed_labels: list[tuple[str, int, str]] = []
        self.operations: list[str] = []
        self.issue_labels = list(initial_labels or [])
        self.issue_comments = list(initial_comments or [])

    @property
    def labels(self) -> list[tuple[str, int, str]]:
        return self.applied_labels

    async def fetch_issue(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        return {"labels": [{"name": label} for label in self.issue_labels]}

    async def list_issue_comments(self, repo_full_name: str, issue_number: int) -> list[dict[str, Any]]:
        return self.issue_comments

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        self.operations.append("post_comment")
        self.comments.append((repo_full_name, issue_number, body))
        if self.fail_on_comment:
            raise self.fail_on_comment
        if self.error:
            raise self.error
        comment = {"id": len(self.issue_comments) + 1, "body": body}
        self.issue_comments.append(comment)
        return comment

    async def update_issue_comment(self, repo_full_name: str, comment_id: int, body: str) -> dict[str, Any]:
        self.operations.append("update_comment")
        self.updated_comments.append((repo_full_name, comment_id, body))
        if self.fail_on_comment:
            raise self.fail_on_comment
        if self.error:
            raise self.error
        for comment in self.issue_comments:
            if comment.get("id") == comment_id:
                comment["body"] = body
                return comment
        return {"id": comment_id, "body": body}

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.operations.append(f"apply:{label}")
        self.applied_labels.append((repo_full_name, issue_number, label))
        if self.fail_on_apply:
            raise self.fail_on_apply
        if self.error:
            raise self.error
        if label not in self.issue_labels:
            self.issue_labels.append(label)
        return {"labels": [label]}

    async def remove_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.operations.append(f"remove:{label}")
        self.removed_labels.append((repo_full_name, issue_number, label))
        if self.fail_on_remove:
            raise self.fail_on_remove
        if self.error:
            raise self.error
        if label in self.issue_labels:
            self.issue_labels.remove(label)
        return {}


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
    assert BB2_STATUS_COMMENT_MARKER in client.comments[0][2]
    assert "## Review Decision" in client.comments[0][2]
    assert "## Review Source" in client.comments[0][2]
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


def test_later_needs_changes_removes_stale_approval_and_review_request_labels() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "abc123"}},
            "labels": [
                {"name": "bb2-approved"},
                {"name": "bb-review-needed"},
                {"name": "agent-verified"},
                {"name": "ready-to-merge"},
            ],
        },
    )
    decision = ReviewDecision(
        decision=ReviewDecisionType.NEEDS_CHANGES,
        confidence=0.9,
        risk_level=RiskLevel.MEDIUM,
        summary="Latest review found a regression.",
        required_changes=["Fix the regression."],
        next_task_prompt="Fix the regression and rerun validation.",
        human_review_required=True,
    )
    response = process_review_work_item(review_work_item_from_parsed(parsed), decision=decision)
    client = FakeWritebackClient(
        initial_labels=["bb2-approved", "bb-review-needed", "agent-verified", "ready-to-merge"]
    )

    result = run(writeback_review_decision(response, client))

    assert result.success is True
    assert result.labels == ["bb2-needs-changes", "agent-next"]
    assert result.removed_labels == ["bb-review-needed", "bb2-approved", "ready-to-merge"]
    assert "bb2-approved" not in client.issue_labels
    assert "bb-review-needed" not in client.issue_labels
    assert "ready-to-merge" not in client.issue_labels
    assert "bb2-needs-changes" in client.issue_labels
    assert "agent-next" in client.issue_labels
    assert client.operations.index("apply:bb2-needs-changes") < client.operations.index("remove:bb2-approved")


def test_later_approval_removes_stale_needs_changes_but_preserves_agent_next_ownership() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "abc123"}},
            "labels": [
                {"name": "bb2-needs-changes"},
                {"name": "agent-next"},
                {"name": "agent-verified"},
            ],
        },
    )
    response = process_review_work_item(review_work_item_from_parsed(parsed))
    client = FakeWritebackClient(initial_labels=["bb2-needs-changes", "agent-next", "agent-verified"])

    result = run(writeback_review_decision(response, client))

    assert result.success is True
    assert result.labels == ["bb2-approved", "ready-to-merge"]
    assert result.removed_labels == ["bb2-needs-changes"]
    assert "bb2-needs-changes" not in client.issue_labels
    assert "agent-next" in client.issue_labels
    assert "bb2-approved" in client.issue_labels
    assert "ready-to-merge" in client.issue_labels
    assert client.operations.index("apply:bb2-approved") < client.operations.index("remove:bb2-needs-changes")


def test_apply_failure_aborts_cleanup_and_leaves_existing_state_intact() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "abc123"}},
            "labels": [{"name": "bb2-approved"}],
        },
    )
    decision = ReviewDecision(
        decision=ReviewDecisionType.NEEDS_CHANGES,
        confidence=0.9,
        risk_level=RiskLevel.MEDIUM,
        summary="Latest review found a regression.",
        required_changes=["Fix the regression."],
        next_task_prompt="Fix the regression and rerun validation.",
        human_review_required=True,
    )
    response = process_review_work_item(review_work_item_from_parsed(parsed), decision=decision)
    client = FakeWritebackClient(
        initial_labels=["bb2-approved"],
        fail_on_apply=RuntimeError("label apply failed"),
    )

    result = run(writeback_review_decision(response, client))

    assert result.success is False
    assert "label apply failed" in result.error
    assert client.issue_labels == ["bb2-approved"]
    assert client.removed_labels == []
    assert not any(operation.startswith("remove:") for operation in client.operations)
    assert "post_comment" not in client.operations
    assert "update_comment" not in client.operations


def test_cleanup_failure_keeps_new_state_and_reports_success() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "abc123"}},
            "labels": [{"name": "bb2-approved"}],
        },
    )
    decision = ReviewDecision(
        decision=ReviewDecisionType.NEEDS_CHANGES,
        confidence=0.9,
        risk_level=RiskLevel.MEDIUM,
        summary="Latest review found a regression.",
        required_changes=["Fix the regression."],
        next_task_prompt="Fix the regression and rerun validation.",
        human_review_required=True,
    )
    response = process_review_work_item(review_work_item_from_parsed(parsed), decision=decision)
    client = FakeWritebackClient(
        initial_labels=["bb2-approved"],
        fail_on_remove=RuntimeError("label cleanup failed"),
    )

    result = run(writeback_review_decision(response, client))

    assert result.success is True
    assert result.removed_labels == []
    assert "bb2-needs-changes" in client.issue_labels
    assert "bb2-approved" in client.issue_labels
    assert client.operations.index("apply:bb2-needs-changes") < client.operations.index("remove:bb2-approved")


def test_comment_failure_does_not_block_label_transition() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "abc123"}},
            "labels": [{"name": "bb2-approved"}],
        },
    )
    decision = ReviewDecision(
        decision=ReviewDecisionType.NEEDS_CHANGES,
        confidence=0.9,
        risk_level=RiskLevel.MEDIUM,
        summary="Latest review found a regression.",
        required_changes=["Fix the regression."],
        next_task_prompt="Fix the regression and rerun validation.",
        human_review_required=True,
    )
    response = process_review_work_item(review_work_item_from_parsed(parsed), decision=decision)
    client = FakeWritebackClient(
        initial_labels=["bb2-approved"],
        fail_on_comment=RuntimeError("comment update failed"),
    )

    result = run(writeback_review_decision(response, client))

    assert result.success is True
    assert result.comment_updated is False
    assert "bb2-needs-changes" in client.issue_labels
    assert "bb2-approved" not in client.issue_labels
    assert client.operations.index("apply:bb2-needs-changes") < client.operations.index("remove:bb2-approved")


def test_existing_bb2_status_comment_is_updated_instead_of_duplicated() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "abc123"}},
        },
    )
    response = process_review_work_item(review_work_item_from_parsed(parsed))
    client = FakeWritebackClient(initial_comments=[{"id": 99, "body": f"{BB2_STATUS_COMMENT_MARKER}\nold"}])

    result = run(writeback_review_decision(response, client))

    assert result.success is True
    assert result.comment_updated is True
    assert result.updated_comment_id == 99
    assert client.comments == []
    assert client.updated_comments == [("riseos/example", 99, result.comment_body)]


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


def test_github_label_apply_error_is_captured() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "abc123"}},
        },
    )
    response = process_review_work_item(review_work_item_from_parsed(parsed))
    client = FakeWritebackClient(fail_on_apply=RuntimeError("GitHub write failed"))

    result = run(writeback_review_decision(response, client))

    assert result.attempted is True
    assert result.success is False
    assert "GitHub write failed" in result.error
    assert client.comments == []
    assert client.removed_labels == []


def test_writeback_protocol_has_no_forbidden_mutation_methods() -> None:
    allowed = {
        "fetch_issue",
        "list_issue_comments",
        "post_issue_comment",
        "update_issue_comment",
        "apply_label",
        "remove_label",
    }
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

    assert BB2_STATUS_COMMENT_MARKER in body
    for section in [
        "Review Decision",
        "Review Source",
        "Risk Level",
        "Summary",
        "Required Changes",
        "Changed Files",
        "Diff Summary",
        "Dry-run Status",
        "Human Review Required",
    ]:
        assert section in body


def test_comment_body_includes_reviewer_model_when_present() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "abc123"}},
        },
    )
    response = process_review_work_item(review_work_item_from_parsed(parsed), reviewer_model="hermes-bb2-runtime-validation")

    body = build_writeback_comment(response)

    assert "## Review Source\nhermes-bb2-runtime-validation" in body
