from __future__ import annotations

import anyio

from app.agent_tasks import AgentTaskCreateRequest, AgentTaskExecutionResult, InMemoryAgentTaskStore, create_agent_task
from app.review_dispatch import build_agent_bus_review_request_payload, dispatch_bb2_review_request_from_execution_result


class FakeAgentBusClient:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def create_work_item(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return {"work_item_id": "review-work-123", "status": "queued"}


def _task():
    task = create_agent_task(
        AgentTaskCreateRequest(
            repo_full_name="marcus937/jarvis-mission-control",
            title="Implement workflow step",
            objective="Make a small change for workflow validation.",
            target_agent="codex-m2",
            correlation_id="wf-shared-123",
        )
    )
    task.agent_bus_work_item_id = "impl-work-123"
    return task


def _result() -> AgentTaskExecutionResult:
    return AgentTaskExecutionResult(
        agent_id="codex-m2",
        status="completed",
        commit_sha="abc123",
        branch="codex-m2/workflow-branch",
        changed_files=["docs/dependency-test.md"],
        evidence={
            "review_dispatch": {
                "tool_preference": [
                    "create_review_packet",
                    "attach_review_to_work_item",
                    "mark_ready_for_review",
                    "dispatch_prompt",
                ],
                "repository": "marcus937/jarvis-mission-control",
                "pr_number": 109,
                "branch": "codex-m2/workflow-branch",
                "base_branch": "agent-integration",
                "work_item_id": "impl-work-123",
                "evidence_packet_id": "evidence-123",
                "review_agent": "BB2",
                "correlation_id": "agtask-not-a-uuid",
                "workflow_id": "wf-shared-123",
                "prompt": "Review this implementation.",
            }
        },
    )


def test_review_request_payload_uses_agent_bus_work_item_contract() -> None:
    task = _task()
    result = _result()

    payload = build_agent_bus_review_request_payload(task, result, result.evidence["review_dispatch"], default_review_agent="bb2")

    assert payload["repository"] == "marcus937/jarvis-mission-control"
    assert payload["pr_number"] == 109
    assert payload["owner_agent"] == "bb2"
    assert payload["review_agent"] == "bb2"
    assert "correlation_id" not in payload
    assert payload["metadata"]["work_item_type"] == "review_request"
    assert payload["metadata"]["queue"] == "review"
    assert payload["metadata"]["source_work_item_id"] == "impl-work-123"
    assert payload["metadata"]["evidence_packet_id"] == "evidence-123"
    assert payload["metadata"]["review_dispatch"]["tool_preference"] == [
        "create_review_packet",
        "attach_review_to_work_item",
    ]


def test_execution_result_dispatch_creates_review_owned_work_item() -> None:
    task = _task()
    result = _result()
    store = InMemoryAgentTaskStore()
    store.save_agent_task(task)
    fake_bus = FakeAgentBusClient()

    async def run_dispatch() -> str | None:
        return await dispatch_bb2_review_request_from_execution_result(
            task,
            result,
            fake_bus,
            review_agent="bb2",
            store=store,
        )

    review_work_item_id = anyio.run(run_dispatch)

    assert review_work_item_id == "review-work-123"
    assert fake_bus.payloads[0]["owner_agent"] == "bb2"
    assert fake_bus.payloads[0]["review_agent"] == "bb2"
    assert fake_bus.payloads[0]["metadata"]["task_type"] == "review_request"
    stored = store.get_agent_task(task.task_id)
    assert stored is not None
    assert stored.execution_evidence["agent_bus_review_work_item_id"] == "review-work-123"
    assert stored.execution_evidence["bb2_review_request_status"] == "queued"


def test_execution_result_dispatch_is_idempotent_when_review_work_item_exists() -> None:
    task = _task()
    task.execution_evidence["agent_bus_review_work_item_id"] = "existing-review-work"
    result = _result()
    store = InMemoryAgentTaskStore()
    store.save_agent_task(task)
    fake_bus = FakeAgentBusClient()

    async def run_dispatch() -> str | None:
        return await dispatch_bb2_review_request_from_execution_result(
            task,
            result,
            fake_bus,
            review_agent="bb2",
            store=store,
        )

    review_work_item_id = anyio.run(run_dispatch)

    assert review_work_item_id == "existing-review-work"
    assert fake_bus.payloads == []
