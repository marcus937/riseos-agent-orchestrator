import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.github_events import GitHubEventType, parse_github_event
from app.github_writeback import writeback_review_decision
from app.reviewer.decision import ReviewDecisionType
from app.review_queue import ReviewWorkItem, process_review_work_item, review_work_item_from_parsed
from app.task_dispatch import dispatch_workflow_chain_continuation, resume_workflow_continuations
from app.workflow_continuation import SQLiteWorkflowContinuationStore, WorkflowContinuationStatus


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeAgentBusDispatchClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.fail = fail

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        if self.fail:
            raise RuntimeError("agent bus unavailable")
        return {"work_item_id": f"generic-chain-{len(self.payloads)}"}


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


def continuation_store(tmp_path: Path) -> SQLiteWorkflowContinuationStore:
    return SQLiteWorkflowContinuationStore(str(tmp_path / "continuations.db"))


def generic_workflow_item(step: str, *, sequence: list[str] | None = None, next_step: str | None = None) -> ReviewWorkItem:
    now = datetime.now(UTC)
    item = ReviewWorkItem(
        id=str(uuid4()),
        created_at=now,
        updated_at=now,
        repo_full_name="marcus937/jarvis-mission-control",
        event_type=GitHubEventType.PULL_REQUEST,
        branch="codex-m2/generic-chain",
        base_branch="agent-integration",
        commit_sha="c" * 40,
        issue_number=77,
        pr_number=88,
        agent_bus_work_item_id=f"agent-bus-{step}",
    )
    review_dispatch: dict[str, Any] = {
        "workflow_chain_id": "generic-chain-1",
        "workflow_step": step,
        "repository": "marcus937/jarvis-mission-control",
        "pr_number": 88,
        "branch": "codex-m2/generic-chain",
        "base_branch": "agent-integration",
        "commit_sha": "d" * 40,
    }
    if sequence is not None:
        review_dispatch["workflow_steps"] = sequence
    if next_step is not None:
        review_dispatch["next_workflow_step"] = next_step
    item.runtime_validation_context = {"workflow_id": "workflow-generic", "review_dispatch": review_dispatch}
    return item


def _review_response_for_generic_step(step: str, sequence: list[str]):
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "marcus937/jarvis-mission-control"},
            "pull_request": {
                "number": 88,
                "head": {"ref": "codex-m2/generic-chain", "sha": "c" * 40},
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
            "workflow_chain_id": "generic-chain-1",
            "workflow_step": step,
            "workflow_steps": sequence,
            "pr_number": 88,
            "branch": "codex-m2/generic-chain",
            "base_branch": "agent-integration",
        }
    }
    return process_review_work_item(item)


def test_generic_three_step_chain_dispatches_step_b_on_same_pr_branch(tmp_path: Path) -> None:
    agent_bus = FakeAgentBusDispatchClient()
    store = continuation_store(tmp_path)

    result = run(
        dispatch_workflow_chain_continuation(
            generic_workflow_item("alpha", sequence=["alpha", "beta", "gamma"]),
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )

    assert result is not None
    assert result.success is True
    payload = agent_bus.payloads[0]
    assert payload["pr_number"] == 88
    assert payload["metadata"]["branch"] == "codex-m2/generic-chain"
    assert payload["metadata"]["workflow_step"] == "beta"
    assert payload["metadata"]["previous_workflow_step"] == "alpha"
    assert payload["metadata"]["workflow_steps"] == ["alpha", "beta", "gamma"]
    assert payload["metadata"]["workflow_sequence"] == ["alpha", "beta", "gamma"]
    assert payload["metadata"]["next_workflow_step"] == "gamma"
    assert payload["metadata"]["create_new_pr"] is False


def test_generic_step_b_dispatches_step_c(tmp_path: Path) -> None:
    agent_bus = FakeAgentBusDispatchClient()

    result = run(
        dispatch_workflow_chain_continuation(
            generic_workflow_item("beta", sequence=["alpha", "beta", "gamma"]),
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=continuation_store(tmp_path),
        )
    )

    assert result is not None
    payload = agent_bus.payloads[0]
    assert payload["metadata"]["workflow_step"] == "gamma"
    assert payload["metadata"]["previous_workflow_step"] == "beta"
    assert payload["metadata"].get("next_workflow_step") is None


def test_generic_final_step_does_not_dispatch_and_allows_ready_to_merge(tmp_path: Path) -> None:
    agent_bus = FakeAgentBusDispatchClient()
    sequence = ["alpha", "beta", "gamma"]

    result = run(
        dispatch_workflow_chain_continuation(
            generic_workflow_item("gamma", sequence=sequence),
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=continuation_store(tmp_path),
        )
    )
    response = _review_response_for_generic_step("gamma", sequence)
    writeback = run(writeback_review_decision(response, FakeWritebackClient(["bb-review-needed"])))

    assert result is None
    assert agent_bus.payloads == []
    assert "ready-to-merge" in writeback.labels


def test_generic_non_final_step_withholds_ready_to_merge() -> None:
    response = _review_response_for_generic_step("alpha", ["alpha", "beta", "gamma"])
    client = FakeWritebackClient(["bb-review-needed"])

    writeback = run(writeback_review_decision(response, client))

    assert writeback.labels == ["bb2-approved"]
    assert "ready-to-merge" not in client.issue_labels


def test_duplicate_generic_approval_does_not_create_duplicate_work_items(tmp_path: Path) -> None:
    agent_bus = FakeAgentBusDispatchClient()
    store = continuation_store(tmp_path)
    item = generic_workflow_item("alpha", sequence=["alpha", "beta", "gamma"])

    first = run(
        dispatch_workflow_chain_continuation(
            item,
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )
    second = run(
        dispatch_workflow_chain_continuation(
            item,
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )

    assert first is not None and second is not None
    assert first.continuation_id == second.continuation_id
    assert first.agent_bus_work_item_id == second.agent_bus_work_item_id
    assert len(agent_bus.payloads) == 1
    assert len(store.list_workflow_continuations("generic-chain-1")) == 1


def test_generic_agent_bus_failure_creates_retry_pending_and_startup_resume_dispatches_once(tmp_path: Path) -> None:
    db_path = tmp_path / "continuations.db"
    store = SQLiteWorkflowContinuationStore(str(db_path))
    failing_agent_bus = FakeAgentBusDispatchClient(fail=True)
    run(
        dispatch_workflow_chain_continuation(
            generic_workflow_item("alpha", sequence=["alpha", "beta", "gamma"]),
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=failing_agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )

    restarted_store = SQLiteWorkflowContinuationStore(str(db_path))
    recovered_agent_bus = FakeAgentBusDispatchClient()
    results = run(resume_workflow_continuations(restarted_store, agent_bus_client=recovered_agent_bus, agent_bus_enabled=True))

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].continuation_status == WorkflowContinuationStatus.DISPATCHED.value
    assert len(recovered_agent_bus.payloads) == 1
    assert recovered_agent_bus.payloads[0]["metadata"]["workflow_steps"] == ["alpha", "beta", "gamma"]


def test_missing_generic_workflow_metadata_fails_closed_without_main_dispatch(tmp_path: Path) -> None:
    agent_bus = FakeAgentBusDispatchClient()
    store = continuation_store(tmp_path)
    item = generic_workflow_item("alpha", sequence=["alpha", "beta", "gamma"])
    item.runtime_validation_context = {
        "source": "runtime_validation_bb2_packet",
        "review_dispatch": {
            "workflow_steps": ["alpha", "beta", "gamma"],
            "repository": "marcus937/jarvis-mission-control",
            "pr_number": 88,
            "branch": "codex-m2/generic-chain",
        },
    }

    result = run(
        dispatch_workflow_chain_continuation(
            item,
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )

    assert result is not None
    assert result.success is False
    assert result.error == "MISSING_WORKFLOW_METADATA"
    assert agent_bus.payloads == []


def test_generic_needs_changes_dispatches_current_step_on_same_pr_branch(tmp_path: Path) -> None:
    agent_bus = FakeAgentBusDispatchClient()

    result = run(
        dispatch_workflow_chain_continuation(
            generic_workflow_item("beta", sequence=["alpha", "beta", "gamma"]),
            ReviewDecisionType.NEEDS_CHANGES,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=continuation_store(tmp_path),
        )
    )

    assert result is not None
    payload = agent_bus.payloads[0]
    assert payload["owner_agent"] == "codex-m2"
    assert payload["review_agent"] == "bb2"
    assert payload["pr_number"] == 88
    assert payload["metadata"]["workflow_step"] == "beta"
    assert payload["metadata"]["previous_workflow_step"] == "beta"
    assert payload["metadata"]["dispatch_reason"] == "workflow_chain_needs_changes"
    assert payload["metadata"]["branch"] == "codex-m2/generic-chain"
    assert payload["metadata"]["create_new_pr"] is False


def test_explicit_next_workflow_step_is_supported_without_sequence(tmp_path: Path) -> None:
    agent_bus = FakeAgentBusDispatchClient()

    result = run(
        dispatch_workflow_chain_continuation(
            generic_workflow_item("alpha", next_step="beta"),
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=continuation_store(tmp_path),
        )
    )

    assert result is not None
    assert result.success is True
    assert agent_bus.payloads[0]["metadata"]["workflow_step"] == "beta"
