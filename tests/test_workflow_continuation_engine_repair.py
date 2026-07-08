from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from app import task_dispatch
from app.github_events import GitHubEventType
from app.reviewer.decision import ReviewDecisionType
from app.review_queue import ReviewWorkItem
from app.workflow_continuation import SQLiteWorkflowContinuationStore, WorkflowContinuationStatus
from app import workflow_continuation_engine_repair as repair


class _AgentTaskStore:
    def __init__(self, tasks: list[object]) -> None:
        self._tasks = tasks

    def list_agent_tasks(self) -> list[object]:
        return list(self._tasks)


class _AgentBusClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def create_work_item(self, payload: dict[str, object]) -> dict[str, object]:
        self.payloads.append(payload)
        return {"work_item_id": "agent-bus-work-item-22"}


def test_continuation_recovers_current_step_from_agent_task_metadata(monkeypatch, tmp_path) -> None:
    workflow_steps = [f"WF{step}" for step in range(21, 30)]
    item = ReviewWorkItem(
        id="review-item-21",
        created_at=datetime.now(UTC),
        repo_full_name="marcus937/jarvis-mission-control",
        event_type=GitHubEventType.PULL_REQUEST,
        branch="circuit/wf21-chain",
        base_branch="agent-integration",
        commit_sha="abc123",
        pr_number=42,
        agent_bus_work_item_id="agent-bus-work-item-21",
        runtime_validation_id="validation-wf21",
        runtime_validation_context={
            "source": "runtime_validation_bb2_packet",
            "validation_id": "validation-wf21",
            "workflow_id": "wf-chain-123",
            "correlation_id": "wf-chain-123",
            "work_item_id": "agent-bus-work-item-21",
            "repo": "marcus937/jarvis-mission-control",
            "pr_number": 42,
            "branch": "circuit/wf21-chain",
            "review_dispatch": {},
        },
    )
    task = SimpleNamespace(
        agent_bus_work_item_id="agent-bus-work-item-21",
        correlation_id="wf-chain-123",
        repo_full_name="marcus937/jarvis-mission-control",
        branch="circuit/wf21-chain",
        execution_evidence={
            "_workflow_chain": {
                "workflow_chain_id": "wf-chain-123",
                "workflow_family": "WF21-WF29",
                "workflow_step": "WF21",
                "current_workflow_step": "WF21",
                "next_workflow_step": "WF22",
                "final_workflow_step": "WF29",
                "workflow_steps": workflow_steps,
                "workflow_sequence": workflow_steps,
                "continuation_mode": "same_pr_branch",
                "merge_gate": "final_step_only",
                "repository": "marcus937/jarvis-mission-control",
                "base_branch": "agent-integration",
            }
        },
    )
    monkeypatch.setattr(repair, "_agent_task_store_for_continuation", lambda: _AgentTaskStore([task]))
    continuation_store = SQLiteWorkflowContinuationStore(str(tmp_path / "state.db"))
    agent_bus_client = _AgentBusClient()

    result = asyncio.run(
        task_dispatch.dispatch_workflow_chain_continuation(
            item,
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus_client,
            agent_bus_enabled=True,
            continuation_store=continuation_store,
            base_branch="agent-integration",
        )
    )

    assert result is not None
    assert result.success is True
    assert item.runtime_validation_context["workflow_chain"]["current_workflow_step"] == "WF21"
    assert item.runtime_validation_context["workflow_chain"]["next_workflow_step"] == "WF22"
    continuations = continuation_store.list_workflow_continuations("wf-chain-123")
    assert len(continuations) == 1
    assert continuations[0].current_workflow_step == "WF21"
    assert continuations[0].next_workflow_step == "WF22"
    assert continuations[0].status == WorkflowContinuationStatus.DISPATCHED
    assert agent_bus_client.payloads
    payload = agent_bus_client.payloads[0]
    assert payload["pr_number"] == 42
    assert payload["metadata"]["workflow_step"] == "WF22"
    assert payload["metadata"]["previous_workflow_step"] == "WF21"
    assert payload["metadata"]["branch"] == "circuit/wf21-chain"
