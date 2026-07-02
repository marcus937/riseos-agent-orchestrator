from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.agent_task_dispatch import build_agent_bus_work_item_payload as build_agent_task_work_item_payload
from app.agent_tasks import (
    AgentTaskCreateRequest,
    AgentTaskExecutionResult,
    AgentTaskStatus,
    InMemoryAgentTaskStore,
    apply_execution_result,
    create_agent_task,
    mark_agent_task_assigned,
)
from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationHermesSummary,
    RuntimeValidationResult,
)
from app.review_dispatch import build_agent_bus_review_request_payload
from app.review_queue import review_queue
from app.reviewer.decision import ReviewDecisionType
from app.runtime_validation_review_bridge import enqueue_review_from_runtime_validation
from app.task_dispatch import dispatch_workflow_chain_continuation
from app.workflow_continuation import SQLiteWorkflowContinuationStore


class _RuntimeBridgeSettings:
    enable_runtime_validation_review_bridge = True


class _ContinuationAgentBusClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"work_item_id": "wi-wf22"}


def test_complete_workflow_chain_context_survives_runtime_review_boundaries_and_dispatches_wf22(tmp_path) -> None:
    review_queue.reset()
    workflow_steps = [f"WF{step}" for step in range(21, 30)]
    workflow_chain = {
        "workflow_chain_id": "wf-chain-123",
        "workflow_family": "WF21-WF29",
        "workflow_steps": workflow_steps,
        "workflow_sequence": workflow_steps,
        "workflow_step": "WF21",
        "current_workflow_step": "WF21",
        "next_workflow_step": "WF22",
        "final_workflow_step": "WF29",
        "continuation_mode": "same_pr_branch",
        "merge_gate": "final_step_only",
        "repository": "marcus937/jarvis-mission-control",
        "base_branch": "agent-integration",
    }
    task = create_agent_task(
        AgentTaskCreateRequest(
            repo_full_name="marcus937/jarvis-mission-control",
            title="WF21 implementation",
            objective="Implement WF21.",
            target_agent="codex-m2",
            correlation_id="wf-chain-123",
        )
    )
    task.execution_evidence = {"_workflow_chain": dict(workflow_chain)}
    task.branch = "codex-m2/wf21-chain"
    mark_agent_task_assigned(task, work_item_id="wi-wf21")

    agent_bus_payload = build_agent_task_work_item_payload(task)
    assert agent_bus_payload["metadata"]["workflow_chain"] == workflow_chain
    assert agent_bus_payload["metadata"]["workflow_step"] == "WF21"
    assert agent_bus_payload["metadata"]["workflow_steps"] == workflow_steps

    codex_result = AgentTaskExecutionResult(
        agent_id="codex-m2",
        status=AgentTaskStatus.COMPLETED,
        commit_sha="abc123",
        branch="codex-m2/wf21-chain",
        changed_files=["src/app.tsx"],
        evidence={
            "evidence_packet_id": "ev-wf21",
            "review_dispatch": {
                "title": "BB2 review for WF21",
                "repository": "marcus937/jarvis-mission-control",
                "pr_number": 38,
                "branch": "codex-m2/wf21-chain",
                "work_item_id": "wi-wf21",
            },
        },
    )
    apply_execution_result(task, codex_result)
    assert task.execution_evidence["_workflow_chain"] == workflow_chain

    review_payload = build_agent_bus_review_request_payload(task, codex_result, codex_result.evidence["review_dispatch"])
    review_metadata = review_payload["metadata"]
    assert review_metadata["workflow_chain"] == workflow_chain
    assert review_metadata["review_dispatch"]["workflow_chain"] == workflow_chain
    assert review_metadata["review_dispatch"]["workflow_step"] == "WF21"

    store = InMemoryAgentTaskStore()
    store.save_agent_task(task)
    now = datetime.now(UTC)
    runtime_result = RuntimeValidationResult(
        validation_id="rv-wf21",
        status="completed",
        repo="marcus937/jarvis-mission-control",
        issue_number=43,
        pr_number=38,
        branch="codex-m2/wf21-chain",
        base_branch="agent-integration",
        work_item_id="wi-wf21",
        evidence_id="ev-wf21",
        review_agent="bb2",
        workflow_id="wf-chain-123",
        review_dispatch={
            "title": "BB2 review for WF21",
            "repository": "marcus937/jarvis-mission-control",
            "pr_number": 38,
            "branch": "codex-m2/wf21-chain",
            "work_item_id": "wi-wf21",
        },
        validation_type="playwright",
        requested_by="agent-bus",
        created_at=now,
        completed_at=now,
        correlation_id="runtime-validation-wf21",
        hermes=RuntimeValidationHermesSummary(
            job_id="job-wf21",
            target_url="https://jarvis-mission-control-gules.vercel.app",
            target_source="vercel_deployment_status",
            status="PASSED",
            manifest_fetched=True,
            bundle_fetched=True,
        ),
        evidence=RuntimeValidationEvidenceSummary(
            page_title="Jarvis Mission Control",
            final_url="https://jarvis-mission-control-gules.vercel.app",
            http_status=200,
            screenshot_present=True,
        ),
        bb2=RuntimeValidationBB2Packet(review_status="approved"),
    )

    review_item = enqueue_review_from_runtime_validation(
        runtime_result,
        _RuntimeBridgeSettings(),
        agent_task_store=store,
    )

    assert review_item is not None
    runtime_context = review_item.runtime_validation_context
    runtime_review_dispatch = runtime_context["review_dispatch"]
    assert runtime_review_dispatch["workflow_chain"] == workflow_chain
    assert runtime_context["workflow_chain"] == workflow_chain
    assert runtime_review_dispatch["workflow_chain_id"] == "wf-chain-123"
    assert runtime_review_dispatch["workflow_step"] == "WF21"
    assert runtime_review_dispatch["current_workflow_step"] == "WF21"
    assert runtime_review_dispatch["workflow_steps"] == workflow_steps
    assert runtime_review_dispatch["workflow_sequence"] == workflow_steps
    assert runtime_review_dispatch["next_workflow_step"] == "WF22"

    agent_bus = _ContinuationAgentBusClient()
    continuation_store = SQLiteWorkflowContinuationStore(str(tmp_path / "continuations.db"))
    first = asyncio.run(
        dispatch_workflow_chain_continuation(
            review_item,
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=continuation_store,
            base_branch="agent-integration",
        )
    )
    duplicate = asyncio.run(
        dispatch_workflow_chain_continuation(
            review_item,
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=continuation_store,
            base_branch="agent-integration",
        )
    )

    assert first is not None and first.success is True
    assert duplicate is not None and duplicate.success is True
    assert len(agent_bus.payloads) == 1
    next_payload = agent_bus.payloads[0]
    assert next_payload["owner_agent"] == "codex-m2"
    assert next_payload["review_agent"] == "bb2"
    assert next_payload["repository"] == "marcus937/jarvis-mission-control"
    assert next_payload["pr_number"] == 38
    assert next_payload["metadata"]["workflow_chain_id"] == "wf-chain-123"
    assert next_payload["metadata"]["workflow_step"] == "WF22"
    assert next_payload["metadata"]["previous_workflow_step"] == "WF21"
    assert next_payload["metadata"]["workflow_sequence"] == workflow_steps
    assert next_payload["metadata"]["workflow_steps"] == workflow_steps
    assert next_payload["metadata"]["next_workflow_step"] == "WF23"
    assert next_payload["metadata"]["branch"] == "codex-m2/wf21-chain"

    continuations = continuation_store.list_workflow_continuations("wf-chain-123")
    assert len(continuations) == 1
    assert continuations[0].current_workflow_step == "WF21"
    assert continuations[0].current_workflow_step != "UNKNOWN"
    assert continuations[0].workflow_chain_id == "wf-chain-123"
    assert continuations[0].workflow_steps == workflow_steps
    assert continuations[0].next_workflow_step == "WF22"
