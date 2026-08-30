import asyncio
from typing import Any

from app.github_events import parse_github_event
from app.github_writeback import writeback_review_decision
from app.review_queue import process_review_work_item, review_work_item_from_parsed


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeWritebackClient:
    def __init__(self, initial_labels: list[str]) -> None:
        self.issue_labels = list(initial_labels)
        self.applied_labels: list[str] = []
        self.removed_labels: list[str] = []
        self.comments: list[str] = []

    async def fetch_issue(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        return {"labels": [{"name": label} for label in self.issue_labels]}

    async def list_issue_comments(self, repo_full_name: str, issue_number: int) -> list[dict[str, Any]]:
        return []

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        self.comments.append(body)
        return {"id": len(self.comments), "body": body}

    async def update_issue_comment(self, repo_full_name: str, comment_id: int, body: str) -> dict[str, Any]:
        self.comments.append(body)
        return {"id": comment_id, "body": body}

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.applied_labels.append(label)
        if label not in self.issue_labels:
            self.issue_labels.append(label)
        return {"labels": [label]}

    async def remove_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.removed_labels.append(label)
        if label in self.issue_labels:
            self.issue_labels.remove(label)
        return {}


def _response_for_workflow_step(step: str):
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "marcus937/jarvis-mission-control"},
            "pull_request": {
                "number": 123,
                "head": {"ref": "codex-m2/wf21-chain", "sha": "a" * 40},
                "base": {"ref": "agent-integration"},
            },
            "labels": [
                {"name": "runtime-agent"},
                {"name": "playwright"},
                {"name": "agent-verified"},
                {"name": "bb-review-needed"},
            ],
        },
    )
    item = review_work_item_from_parsed(parsed)
    item.runtime_validation_context = {
        "review_dispatch": {
            "workflow_chain_id": "wf-chain-123",
            "workflow_step": step,
            "pr_number": 123,
            "branch": "codex-m2/wf21-chain",
            "base_branch": "agent-integration",
        }
    }
    return process_review_work_item(item)


def test_wf21_to_wf28_approval_does_not_add_ready_to_merge() -> None:
    response = _response_for_workflow_step("WF21")
    client = FakeWritebackClient(["runtime-agent", "playwright", "agent-verified", "bb-review-needed"])

    result = run(writeback_review_decision(response, client))

    assert result.success is True
    assert result.labels == ["bb2-approved"]
    assert "ready-to-merge" not in client.issue_labels
    assert "ready-to-merge" not in client.applied_labels


def test_wf29_approval_allows_ready_to_merge() -> None:
    response = _response_for_workflow_step("WF29")
    client = FakeWritebackClient(["runtime-agent", "playwright", "agent-verified", "bb-review-needed"])

    result = run(writeback_review_decision(response, client))

    assert result.success is True
    assert result.labels == ["bb2-approved", "ready-to-merge"]
    assert "ready-to-merge" in client.issue_labels
    assert "ready-to-merge" in client.applied_labels
