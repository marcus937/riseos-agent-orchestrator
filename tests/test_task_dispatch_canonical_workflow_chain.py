from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.github_events import GitHubEventType
from app.review_queue import ReviewWorkItem
from app.reviewer.decision import ReviewDecisionType
from app.task_dispatch import dispatch_workflow_chain_continuation
from app.workflow_continuation import SQLiteWorkflowContinuationStore


class _AgentBusClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"work_item_id": "wi-wf22"}


def test_continuation_reads_canonical_workflow_chain_without_flat_aliases(tmp_path) -> None:
    workflow_sequence = [{"task_key": f"WF{step}", "title": f"WF{step}"} for step in range(21, 30)]
    workflow_chain = {
        "workflow_chain_id": "wf-chain-123",
        "workflow_family": "WF21-WF29",
        "workflow_sequence": workflow_sequence,
        "workflow_step": "WF21",
        "current_workflow_step": "WF21",
        "next_workflow_step": "WF22",
        "final_workflow_step": "WF29",
        "continuation_mode": "same_pr_branch",
        "merge_gate": "final_step_only",
        "repository": "marcus937/jarvis-mission-control",
        "pr_number": 38,
        "branch": "codex-m2/wf21-chain",
        "base_branch": "agent-integration",
        "work_item_id": "wi-wf21",
    }
    item = ReviewWorkItem(
        id="review-wf21",
        created_at=datetime.now(UTC),
        repo_full_name="marcus937/jarvis-mission-control",
        event_type=GitHubEventType.PULL_REQUEST,
        branch="codex-m2/wf21-chain",
        base_branch="agent-integration",
        pr_number=38,
        agent_bus_work_item_id="wi-wf21",
        runtime_validation_context={
            "source": "runtime_validation_bb2_packet",
            "workflow_chain": workflow_chain,
            "review_dispatch": {"workflow_chain": workflow_chain},
        },
    )

    agent_bus = _AgentBusClient()
    continuation_store = SQLiteWorkflowContinuationStore(str(tmp_path / "continuations.db"))
    result = asyncio.run(
        dispatch_workflow_chain_continuation(
            item,
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=continuation_store,
            base_branch="agent-integration",
        )
    )

    assert result is not None
    assert result.success is True
    assert result.continuation_status == "DISPATCHED"
    assert len(agent_bus.payloads) == 1
    payload = agent_bus.payloads[0]
    assert payload["repository"] == "marcus937/jarvis-mission-control"
    assert payload["pr_number"] == 38
    assert payload["metadata"]["workflow_chain_id"] == "wf-chain-123"
    assert payload["metadata"]["workflow_step"] == "WF22"
    assert payload["metadata"]["previous_workflow_step"] == "WF21"
    assert payload["metadata"]["workflow_steps"] == [f"WF{step}" for step in range(21, 30)]
    assert payload["metadata"]["workflow_sequence"] == [f"WF{step}" for step in range(21, 30)]
    assert payload["metadata"]["workflow_chain"]["workflow_step"] == "WF22"
    assert payload["metadata"]["workflow_chain"]["current_workflow_step"] == "WF22"
    assert payload["metadata"]["workflow_chain"]["next_workflow_step"] == "WF23"

    continuations = continuation_store.list_workflow_continuations("wf-chain-123")
    assert len(continuations) == 1
    assert continuations[0].workflow_chain_id == "wf-chain-123"
    assert continuations[0].current_workflow_step == "WF21"
    assert continuations[0].current_workflow_step != "UNKNOWN"
    assert continuations[0].next_workflow_step == "WF22"
    assert continuations[0].workflow_steps == [f"WF{step}" for step in range(21, 30)]
