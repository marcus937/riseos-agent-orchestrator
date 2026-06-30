import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.clients.github import GitHubClient
from app.github_events import GitHubEventType
from app.reviewer.decision import ReviewDecisionType
from app.review_queue import ReviewWorkItem
from app.task_dispatch import (
    LABEL_AGENT_NEXT,
    AgentTaskIssue,
    build_circuit_assignment_body,
    dispatch_next_agent_task,
    dispatch_workflow_chain_continuation,
    list_agent_ready_issues,
    resume_workflow_continuations,
    select_next_agent_task,
    should_dispatch_next_task,
)
from app.workflow_continuation import SQLiteWorkflowContinuationStore, WorkflowContinuationStatus


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeTaskDispatchClient:
    def __init__(self, issues: list[dict[str, Any]] | None = None) -> None:
        self.issues = issues or []
        self.list_calls: list[tuple[str, list[str] | None, str, str]] = []
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str]] = []
        self.actions: list[tuple[str, int, str]] = []

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
        self.actions.append(("comment", issue_number, body))
        return {"id": 1}

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.labels.append((repo_full_name, issue_number, label))
        self.actions.append(("label", issue_number, label))
        return {"labels": [label]}


class FakeAgentBusDispatchClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.fail = fail

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        if self.fail:
            raise RuntimeError("agent bus unavailable")
        return {"work_item_id": f"workflow-chain-{len(self.payloads)}"}


def issue(number: int, *, created_at: str, labels: list[str], title: str | None = None, body: str | None = None) -> dict[str, Any]:
    return {
        "number": number,
        "title": title or f"Task {number}",
        "body": body or "Do the thing.",
        "created_at": created_at,
        "labels": [{"name": label} for label in labels],
    }


def continuation_store(tmp_path: Path) -> SQLiteWorkflowContinuationStore:
    return SQLiteWorkflowContinuationStore(str(tmp_path / "continuations.db"))


def workflow_item(step: str = "WF21") -> ReviewWorkItem:
    now = datetime.now(UTC)
    item = ReviewWorkItem(
        id=str(uuid4()),
        created_at=now,
        updated_at=now,
        repo_full_name="marcus937/jarvis-mission-control",
        event_type=GitHubEventType.PULL_REQUEST,
        branch="codex-m2/wf21-chain",
        base_branch="agent-integration",
        commit_sha="a" * 40,
        issue_number=321,
        pr_number=123,
        agent_bus_work_item_id="agent-bus-wf21",
    )
    item.runtime_validation_context = {
        "workflow_id": "workflow-123",
        "review_dispatch": {
            "workflow_chain_id": "wf-chain-123",
            "workflow_step": step,
            "repository": "marcus937/jarvis-mission-control",
            "pr_number": 123,
            "branch": "codex-m2/wf21-chain",
            "base_branch": "agent-integration",
            "commit_sha": "b" * 40,
        },
    }
    return item


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


def test_no_unclaimed_ready_issue_is_handled_cleanly() -> None:
    client = FakeTaskDispatchClient([])

    result = run(dispatch_next_agent_task("riseos/example", client, enabled=True))

    assert result.attempted is True
    assert result.success is False
    assert result.issue_number is None
    assert result.error == "No queued unclaimed agent-ready issue found"
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
    assert client.actions[0] == ("label", 8, LABEL_AGENT_NEXT)
    assert client.actions[1][0:2] == ("comment", 8)
    body = client.comments[0][2]
    assert "Circuit Assignment" in body
    assert "Wire dispatch" in body
    assert "Target integration branch: `agent-integration`" in body
    assert "Working branch: create a dedicated `circuit/<task>` branch" in body
    assert "Open a PR into `agent-integration`" in body
    assert "Request BB2 review" in body
    assert "Never commit directly to `main`" in body
    assert "Never merge or deploy" in body
    assert "PR URL and completed commit SHA" in body
    assert "Implement task dispatch." in body


def test_wf21_approval_dispatches_wf22_on_same_pr_branch(tmp_path: Path) -> None:
    agent_bus = FakeAgentBusDispatchClient()
    store = continuation_store(tmp_path)

    result = run(
        dispatch_workflow_chain_continuation(
            workflow_item("WF21"),
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )

    assert result is not None
    assert result.success is True
    assert result.agent_bus_work_item_id == "workflow-chain-1"
    assert result.continuation_id is not None
    payload = agent_bus.payloads[0]
    assert payload["owner_agent"] == "codex-m2"
    assert payload["review_agent"] == "bb2"
    assert payload["repository"] == "marcus937/jarvis-mission-control"
    assert payload["pr_number"] == 123
    assert payload["metadata"]["workflow_step"] == "WF22"
    assert payload["metadata"]["previous_workflow_step"] == "WF21"
    assert payload["metadata"]["branch"] == "codex-m2/wf21-chain"
    assert payload["metadata"]["previous_work_item_id"] == "agent-bus-wf21"
    assert payload["metadata"]["continuation_id"] == result.continuation_id
    rows = store.list_workflow_continuations("wf-chain-123")
    assert len(rows) == 1
    assert rows[0].status == WorkflowContinuationStatus.DISPATCHED


def test_wf22_continuation_does_not_target_main_or_create_fresh_branch(tmp_path: Path) -> None:
    agent_bus = FakeAgentBusDispatchClient()

    run(
        dispatch_workflow_chain_continuation(
            workflow_item("WF21"),
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=continuation_store(tmp_path),
        )
    )

    metadata = agent_bus.payloads[0]["metadata"]
    assert metadata["workflow_step"] == "WF22"
    assert metadata["base_branch"] == "agent-integration"
    assert metadata["branch"] == "codex-m2/wf21-chain"
    assert metadata["branch"] != "main"
    assert metadata["reuse_existing_pr"] is True
    assert metadata["create_new_pr"] is False
    assert metadata["open_new_pr"] is False
    assert metadata["merge_required_before_next_step"] is False


def test_wf29_approval_does_not_dispatch_followup_work_item(tmp_path: Path) -> None:
    agent_bus = FakeAgentBusDispatchClient()

    result = run(
        dispatch_workflow_chain_continuation(
            workflow_item("WF29"),
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=continuation_store(tmp_path),
        )
    )

    assert result is None
    assert agent_bus.payloads == []


def test_needs_changes_returns_to_codex_m2_on_same_pr_branch(tmp_path: Path) -> None:
    agent_bus = FakeAgentBusDispatchClient()

    result = run(
        dispatch_workflow_chain_continuation(
            workflow_item("WF24"),
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
    assert payload["pr_number"] == 123
    assert payload["metadata"]["workflow_step"] == "WF24"
    assert payload["metadata"]["previous_workflow_step"] == "WF24"
    assert payload["metadata"]["dispatch_reason"] == "workflow_chain_needs_changes"
    assert payload["metadata"]["branch"] == "codex-m2/wf21-chain"
    assert payload["metadata"]["create_new_pr"] is False


def test_duplicate_approval_reuses_existing_continuation(tmp_path: Path) -> None:
    agent_bus = FakeAgentBusDispatchClient()
    store = continuation_store(tmp_path)
    item = workflow_item("WF21")

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
    assert len(store.list_workflow_continuations("wf-chain-123")) == 1


def test_agent_bus_outage_preserves_retry_pending_continuation(tmp_path: Path) -> None:
    store = continuation_store(tmp_path)
    failing_agent_bus = FakeAgentBusDispatchClient(fail=True)

    result = run(
        dispatch_workflow_chain_continuation(
            workflow_item("WF21"),
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=failing_agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )

    assert result is not None
    assert result.success is False
    assert result.continuation_status == WorkflowContinuationStatus.RETRY_PENDING.value
    rows = store.list_workflow_continuations("wf-chain-123")
    assert len(rows) == 1
    assert rows[0].status == WorkflowContinuationStatus.RETRY_PENDING
    assert rows[0].last_error == "agent bus unavailable"


def test_orchestrator_restart_resumes_retry_pending_continuation(tmp_path: Path) -> None:
    db_path = tmp_path / "continuations.db"
    store = SQLiteWorkflowContinuationStore(str(db_path))
    failing_agent_bus = FakeAgentBusDispatchClient(fail=True)
    run(
        dispatch_workflow_chain_continuation(
            workflow_item("WF21"),
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=failing_agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )

    restarted_store = SQLiteWorkflowContinuationStore(str(db_path))
    recovered_agent_bus = FakeAgentBusDispatchClient()
    results = run(
        resume_workflow_continuations(
            restarted_store,
            agent_bus_client=recovered_agent_bus,
            agent_bus_enabled=True,
        )
    )

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].continuation_status == WorkflowContinuationStatus.DISPATCHED.value
    assert len(restarted_store.list_workflow_continuations("wf-chain-123")) == 1
    assert len(recovered_agent_bus.payloads) == 1


def test_duplicate_retry_reuses_existing_continuation(tmp_path: Path) -> None:
    store = continuation_store(tmp_path)
    failing_agent_bus = FakeAgentBusDispatchClient(fail=True)
    item = workflow_item("WF22")
    run(
        dispatch_workflow_chain_continuation(
            item,
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=failing_agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )

    recovered_agent_bus = FakeAgentBusDispatchClient()
    first_retry = run(
        dispatch_workflow_chain_continuation(
            item,
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=recovered_agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )
    second_retry = run(
        dispatch_workflow_chain_continuation(
            item,
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=recovered_agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )

    assert first_retry is not None and second_retry is not None
    assert first_retry.continuation_id == second_retry.continuation_id
    assert len(recovered_agent_bus.payloads) == 1
    rows = store.list_workflow_continuations("wf-chain-123")
    assert len(rows) == 1
    assert rows[0].status == WorkflowContinuationStatus.DISPATCHED


def test_needs_changes_reuses_existing_continuation(tmp_path: Path) -> None:
    store = continuation_store(tmp_path)
    agent_bus = FakeAgentBusDispatchClient()
    item = workflow_item("WF24")
    first = run(
        dispatch_workflow_chain_continuation(
            item,
            ReviewDecisionType.NEEDS_CHANGES,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )
    second = run(
        dispatch_workflow_chain_continuation(
            item,
            ReviewDecisionType.NEEDS_CHANGES,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )

    assert first is not None and second is not None
    assert first.continuation_id == second.continuation_id
    assert second.continuation_status == WorkflowContinuationStatus.CHANGES_REQUESTED.value
    assert len(agent_bus.payloads) == 1
    assert len(store.list_workflow_continuations("wf-chain-123")) == 1


def test_continuation_survives_wf21_to_wf23_progression(tmp_path: Path) -> None:
    store = continuation_store(tmp_path)
    agent_bus = FakeAgentBusDispatchClient()

    run(
        dispatch_workflow_chain_continuation(
            workflow_item("WF21"),
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )
    run(
        dispatch_workflow_chain_continuation(
            workflow_item("WF22"),
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=store,
        )
    )

    rows = store.list_workflow_continuations("wf-chain-123")
    assert [row.next_workflow_step for row in rows] == ["WF22", "WF23"]
    assert len(agent_bus.payloads) == 2


def test_missing_workflow_metadata_never_dispatches_fresh_task(tmp_path: Path) -> None:
    store = continuation_store(tmp_path)
    agent_bus = FakeAgentBusDispatchClient()
    item = workflow_item("WF21")
    item.runtime_validation_context = {
        "source": "runtime_validation_bb2_packet",
        "review_dispatch": {
            "workflow_step": "WF21",
            "repository": "marcus937/jarvis-mission-control",
            "pr_number": 123,
            "branch": "codex-m2/wf21-chain",
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
    assert result.continuation_status == WorkflowContinuationStatus.FAILED.value
    assert agent_bus.payloads == []


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
    assert "Target integration branch: `agent-integration`" in body
    assert "Working branch: create a dedicated `circuit/<task>` branch" in body
    assert "Work only on the dedicated `circuit/<task>` branch" in body
    assert "Open a PR into `agent-integration`" in body
    assert "Never commit directly to `main`" in body
    assert "Never merge or deploy" in body
    assert "Task body here." in body


def test_assignment_comment_body_removes_legacy_branch_blockers() -> None:
    body = build_circuit_assignment_body(
        AgentTaskIssue(
            number=10,
            title="Branch policy",
            body="Task body here.",
            labels=["agent-task", "agent-ready"],
        )
    )

    assert "agent-integration` only" not in body
    assert "Stay on `agent-integration`" not in body
    assert "Do not open a PR unless explicitly requested" not in body
    assert "Do not mutate branches" not in body


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


def test_list_agent_ready_issues_filters_existing_owner_labels() -> None:
    client = FakeTaskDispatchClient(
        [
            issue(1, created_at="2026-06-01T00:00:00Z", labels=["agent-task", "agent-ready", "agent-next"]),
            issue(2, created_at="2026-06-02T00:00:00Z", labels=["agent-task", "agent-ready", "agent-working"]),
            issue(3, created_at="2026-06-03T00:00:00Z", labels=["agent-task", "agent-ready"]),
        ]
    )

    ready = run(list_agent_ready_issues("riseos/example", client))

    assert [item.number for item in ready] == [3]


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
