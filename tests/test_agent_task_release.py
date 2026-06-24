import asyncio
from typing import Any

from app.agent_task_release import CIRCUIT_WAKEUP_EVENT, dispatch_circuit_wakeup_for_assigned_task, release_runnable_agent_tasks
from app.agent_tasks import (
    AgentTask,
    AgentTaskCreateRequest,
    AgentTaskStatus,
    InMemoryAgentTaskStore,
    append_lifecycle_event,
    create_agent_task,
    mark_agent_task_assigned,
)
from app.circuit_agent_trigger import CircuitAgentTriggerResult
from app.config import Settings


class FakeAgentBusClient:
    def __init__(self, work_item_id: str = "work-item-123") -> None:
        self.work_item_id = work_item_id
        self.payloads: list[dict[str, Any]] = []
        self.visibility_checks: list[str] = []

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"work_item_id": self.work_item_id, "status": "queued"}

    async def get_work_item(self, work_item_id: str) -> dict[str, Any]:
        self.visibility_checks.append(work_item_id)
        return {"work_item_id": work_item_id, "status": "queued"}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def circuit_settings() -> Settings:
    return Settings(
        circuit_agent_trigger_url="https://api.chatgpt.com/v1/workspace_agents/agent-id/trigger",
        circuit_agent_access_token="secret-token",
    )


def make_task(*, target_agent: str = "circuit-forge", workflow_id: str = "wf-test-123") -> AgentTask:
    return create_agent_task(
        AgentTaskCreateRequest(
            repo_full_name="marcus937/riseos-agent-orchestrator",
            title="Circuit Dispatch Test",
            objective="Wake up and report inbox contents.",
            target_agent=target_agent,
            correlation_id=workflow_id,
        )
    )


def test_release_wakes_circuit_after_agent_bus_assignment_is_persisted(monkeypatch) -> None:
    store = InMemoryAgentTaskStore()
    task = make_task()
    store.save_agent_task(task)
    bus = FakeAgentBusClient()
    wake_calls: list[dict[str, Any]] = []

    async def fake_wake_circuit_agent_for_work(settings: Settings, **kwargs: Any) -> CircuitAgentTriggerResult:
        persisted = store.get_agent_task(task.task_id)
        assert persisted is not None
        assert persisted.status == AgentTaskStatus.ASSIGNED
        assert persisted.agent_bus_work_item_id == "work-item-123"
        wake_calls.append({"settings": settings, **kwargs})
        return CircuitAgentTriggerResult(attempted=True, success=True, status_code=202)

    monkeypatch.setattr("app.agent_task_release.wake_circuit_agent_for_work", fake_wake_circuit_agent_for_work)

    released = run(release_runnable_agent_tasks(store, bus, settings=circuit_settings()))

    assert len(released) == 1
    assigned = store.get_agent_task(task.task_id)
    assert assigned is not None
    assert assigned.status == AgentTaskStatus.ASSIGNED
    assert assigned.agent_bus_work_item_id == "work-item-123"
    assert bus.payloads[0]["owner_agent"] == "circuit-forge"
    assert bus.visibility_checks == ["work-item-123"]
    assert len(wake_calls) == 1
    assert wake_calls[0]["target_agent"] == "circuit-forge"
    assert wake_calls[0]["repo_full_name"] == "marcus937/riseos-agent-orchestrator"
    assert wake_calls[0]["issue_number"] is None
    assert wake_calls[0]["workflow_id"] == "wf-test-123"
    assert wake_calls[0]["work_item_id"] == "work-item-123"
    assert any(
        event.event == CIRCUIT_WAKEUP_EVENT
        and event.metadata.get("agent_bus_work_item_id") == "work-item-123"
        and event.metadata.get("success") is True
        for event in assigned.lifecycle_events
    )


def test_circuit_wakeup_is_not_attempted_before_work_item_id_exists(monkeypatch) -> None:
    task = make_task()
    wake_calls: list[dict[str, Any]] = []

    async def fake_wake_circuit_agent_for_work(settings: Settings, **kwargs: Any) -> CircuitAgentTriggerResult:
        wake_calls.append(kwargs)
        return CircuitAgentTriggerResult(attempted=True, success=True, status_code=202)

    monkeypatch.setattr("app.agent_task_release.wake_circuit_agent_for_work", fake_wake_circuit_agent_for_work)

    run(dispatch_circuit_wakeup_for_assigned_task(task, settings=circuit_settings(), agent_bus_client=FakeAgentBusClient()))

    assert wake_calls == []
    assert not any(event.event == CIRCUIT_WAKEUP_EVENT for event in task.lifecycle_events)


def test_circuit_wakeup_runs_once_per_work_item_id(monkeypatch) -> None:
    task = make_task()
    mark_agent_task_assigned(task, work_item_id="work-item-123")
    wake_calls: list[dict[str, Any]] = []

    async def fake_wake_circuit_agent_for_work(settings: Settings, **kwargs: Any) -> CircuitAgentTriggerResult:
        wake_calls.append(kwargs)
        return CircuitAgentTriggerResult(attempted=True, success=True, status_code=202)

    monkeypatch.setattr("app.agent_task_release.wake_circuit_agent_for_work", fake_wake_circuit_agent_for_work)

    run(dispatch_circuit_wakeup_for_assigned_task(task, settings=circuit_settings(), agent_bus_client=FakeAgentBusClient()))
    run(dispatch_circuit_wakeup_for_assigned_task(task, settings=circuit_settings(), agent_bus_client=FakeAgentBusClient()))

    assert [call["work_item_id"] for call in wake_calls] == ["work-item-123"]


def test_circuit_wakeup_can_run_again_for_new_circuit_assignment(monkeypatch) -> None:
    task = make_task()
    mark_agent_task_assigned(task, work_item_id="work-item-123")
    wake_calls: list[dict[str, Any]] = []

    async def fake_wake_circuit_agent_for_work(settings: Settings, **kwargs: Any) -> CircuitAgentTriggerResult:
        wake_calls.append(kwargs)
        return CircuitAgentTriggerResult(attempted=True, success=True, status_code=202)

    monkeypatch.setattr("app.agent_task_release.wake_circuit_agent_for_work", fake_wake_circuit_agent_for_work)

    run(dispatch_circuit_wakeup_for_assigned_task(task, settings=circuit_settings(), agent_bus_client=FakeAgentBusClient()))
    append_lifecycle_event(task, "bb2_changes_requested", metadata={"route_to": "circuit-forge"})
    task.agent_bus_work_item_id = "work-item-456"
    run(dispatch_circuit_wakeup_for_assigned_task(task, settings=circuit_settings(), agent_bus_client=FakeAgentBusClient("work-item-456")))

    assert [call["work_item_id"] for call in wake_calls] == ["work-item-123", "work-item-456"]


def test_circuit_target_agent_aliases_trigger_wakeup(monkeypatch) -> None:
    wake_calls: list[dict[str, Any]] = []

    async def fake_wake_circuit_agent_for_work(settings: Settings, **kwargs: Any) -> CircuitAgentTriggerResult:
        wake_calls.append(kwargs)
        return CircuitAgentTriggerResult(attempted=True, success=True, status_code=202)

    monkeypatch.setattr("app.agent_task_release.wake_circuit_agent_for_work", fake_wake_circuit_agent_for_work)

    for alias in ("circuit", "circuit-forge"):
        task = make_task(target_agent=alias, workflow_id=f"wf-{alias}")
        mark_agent_task_assigned(task, work_item_id=f"work-item-{alias}")
        run(dispatch_circuit_wakeup_for_assigned_task(task, settings=circuit_settings(), agent_bus_client=FakeAgentBusClient(f"work-item-{alias}")))

    assert [call["target_agent"] for call in wake_calls] == ["circuit", "circuit-forge"]
    assert [call["work_item_id"] for call in wake_calls] == ["work-item-circuit", "work-item-circuit-forge"]


def test_non_circuit_assignment_does_not_trigger_wakeup(monkeypatch) -> None:
    task = make_task(target_agent="codex-m2")
    mark_agent_task_assigned(task, work_item_id="work-item-123")
    wake_calls: list[dict[str, Any]] = []

    async def fake_wake_circuit_agent_for_work(settings: Settings, **kwargs: Any) -> CircuitAgentTriggerResult:
        wake_calls.append(kwargs)
        return CircuitAgentTriggerResult(attempted=True, success=True, status_code=202)

    monkeypatch.setattr("app.agent_task_release.wake_circuit_agent_for_work", fake_wake_circuit_agent_for_work)

    run(dispatch_circuit_wakeup_for_assigned_task(task, settings=circuit_settings(), agent_bus_client=FakeAgentBusClient()))

    assert wake_calls == []
