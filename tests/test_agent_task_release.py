import asyncio
from typing import Any

from app.agent_task_release import release_runnable_agent_tasks
from app.agent_tasks import AgentTaskCreateRequest, AgentTaskStatus, InMemoryAgentTaskStore, create_agent_task
from app.circuit_agent_trigger import CircuitAgentTriggerResult
from app.config import Settings


class FakeAgentBusClient:
    def __init__(self, work_item_id: str = "work-item-123") -> None:
        self.work_item_id = work_item_id
        self.payloads: list[dict[str, Any]] = []

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"work_item_id": self.work_item_id, "status": "queued"}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_release_wakes_circuit_after_agent_bus_assignment(monkeypatch) -> None:
    store = InMemoryAgentTaskStore()
    task = create_agent_task(
        AgentTaskCreateRequest(
            repo_full_name="marcus937/riseos-agent-orchestrator",
            title="Circuit Dispatch Test",
            objective="Wake up and report inbox contents.",
            target_agent="circuit-forge",
            correlation_id="wf-test-123",
        )
    )
    store.save_agent_task(task)
    bus = FakeAgentBusClient()
    wake_calls: list[dict[str, Any]] = []

    async def fake_wake_circuit_agent_for_work(settings: Settings, **kwargs: Any) -> CircuitAgentTriggerResult:
        wake_calls.append({"settings": settings, **kwargs})
        return CircuitAgentTriggerResult(attempted=True, success=True, status_code=202)

    monkeypatch.setattr("app.agent_task_release.wake_circuit_agent_for_work", fake_wake_circuit_agent_for_work)

    released = run(
        release_runnable_agent_tasks(
            store,
            bus,
            settings=Settings(
                circuit_agent_trigger_url="https://api.chatgpt.com/v1/workspace_agents/agent-id/trigger",
                circuit_agent_access_token="secret-token",
            ),
        )
    )

    assert len(released) == 1
    assigned = store.get_agent_task(task.task_id)
    assert assigned is not None
    assert assigned.status == AgentTaskStatus.ASSIGNED
    assert assigned.agent_bus_work_item_id == "work-item-123"
    assert bus.payloads[0]["owner_agent"] == "circuit-forge"
    assert wake_calls[0]["target_agent"] == "circuit-forge"
    assert wake_calls[0]["repo_full_name"] == "marcus937/riseos-agent-orchestrator"
    assert wake_calls[0]["workflow_id"] == "wf-test-123"
