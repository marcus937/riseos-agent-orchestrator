import asyncio
from typing import Any

from app.agent_task_release import release_runnable_agent_tasks
from app.agent_tasks import AgentTaskCreateRequest, InMemoryAgentTaskStore, create_agent_task
from app.config import Settings
from app.engineering_workforce import (
    CIRCUIT_FORGE,
    CODEX_M2,
    SCHEDULER_METADATA_KEY,
    apply_scheduler_decision,
    normalize_engineering_agent,
    schedule_engineering_workforce,
)
from app.workflow_orchestration import InMemoryWorkflowStore, WorkflowCreateRequest, WorkflowTask, create_workflow


class FakeWorkforceAgentBusClient:
    def __init__(
        self,
        *,
        status: dict[str, Any] | None = None,
        queue: list[dict[str, Any]] | None = None,
        work_item_id: str = "work-item-123",
        status_error: Exception | None = None,
    ) -> None:
        self.status = status if status is not None else {"status": "online", "health_state": "healthy", "availability": "available"}
        self.queue = queue if queue is not None else []
        self.work_item_id = work_item_id
        self.status_error = status_error
        self.payloads: list[dict[str, Any]] = []

    async def get_agent_status(self, agent_id: str) -> dict[str, Any]:
        if self.status_error is not None:
            raise self.status_error
        return self.status

    async def get_agent_queue(self, agent_id: str) -> list[dict[str, Any]]:
        return self.queue

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"work_item_id": self.work_item_id}

    async def get_work_item(self, work_item_id: str) -> dict[str, Any]:
        return {"work_item_id": work_item_id, "status": "queued"}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def make_task(*, target_agent: str = "engineering", objective: str = "Implement a backend endpoint."):
    return create_agent_task(
        AgentTaskCreateRequest(
            repo_full_name="marcus937/riseos-agent-orchestrator",
            title="Engineering task",
            objective=objective,
            target_agent=target_agent,
            correlation_id="wf-test-123",
        )
    )


def scheduler_settings(**overrides: Any) -> Settings:
    values = {"enable_engineering_workforce_scheduler": True, **overrides}
    return Settings(**values)


def test_normalizes_engineering_worker_aliases() -> None:
    assert normalize_engineering_agent("codex") == CODEX_M2
    assert normalize_engineering_agent("Codex") == CODEX_M2
    assert normalize_engineering_agent("codex-m2") == CODEX_M2
    assert normalize_engineering_agent("circuit") == CIRCUIT_FORGE
    assert normalize_engineering_agent("Circuit Forge") == CIRCUIT_FORGE
    assert normalize_engineering_agent("circuit-forge") == CIRCUIT_FORGE


def test_scheduler_disabled_by_default_preserves_generic_target() -> None:
    task = make_task(target_agent="engineering")

    decision = run(schedule_engineering_workforce(task, Settings(), signal_client=FakeWorkforceAgentBusClient()))

    assert decision.applied is False
    assert decision.scheduler_enabled is False
    assert task.target_agent == "engineering"


def test_explicit_codex_target_is_preserved_when_scheduler_enabled() -> None:
    task = make_task(target_agent="codex-m2")

    decision = run(schedule_engineering_workforce(task, scheduler_settings(), signal_client=FakeWorkforceAgentBusClient()))

    assert decision.applied is False
    assert decision.reason == "explicit_target_agent"
    assert decision.selected_agent == CODEX_M2
    assert task.target_agent == "codex-m2"


def test_explicit_circuit_target_is_preserved_when_scheduler_enabled() -> None:
    task = make_task(target_agent="circuit-forge")

    decision = run(schedule_engineering_workforce(task, scheduler_settings(), signal_client=FakeWorkforceAgentBusClient()))

    assert decision.applied is False
    assert decision.reason == "explicit_target_agent"
    assert decision.selected_agent == CIRCUIT_FORGE
    assert task.target_agent == "circuit-forge"


def test_explicit_alias_target_is_canonicalized_before_dispatch() -> None:
    task = make_task(target_agent="Circuit Forge")

    decision = run(schedule_engineering_workforce(task, scheduler_settings(), signal_client=FakeWorkforceAgentBusClient()))
    apply_scheduler_decision(task, decision)

    assert decision.applied is True
    assert decision.scheduler_mode is False
    assert decision.reason == "canonicalized_explicit_target_agent"
    assert task.target_agent == CIRCUIT_FORGE


def test_scheduler_only_runs_for_generic_engineering_targets() -> None:
    task = make_task(target_agent="bb2")

    decision = run(schedule_engineering_workforce(task, scheduler_settings(), signal_client=FakeWorkforceAgentBusClient()))

    assert decision.applied is False
    assert decision.scheduler_mode is False
    assert decision.reason == "target_agent_not_engineering_generic"
    assert task.target_agent == "bb2"


def test_omitted_workflow_target_agent_invokes_scheduler() -> None:
    agent_store = InMemoryAgentTaskStore()
    workflow_store = InMemoryWorkflowStore()
    workflow_task = WorkflowTask(task_key="WF1", title="Backend task", objective="Implement a backend endpoint.")
    assert "target_agent" not in workflow_task.model_fields_set

    create_workflow(
        WorkflowCreateRequest(
            repo_full_name="marcus937/riseos-agent-orchestrator",
            title="Omitted target workflow",
            tasks=[workflow_task],
        ),
        workflow_store=workflow_store,
        agent_task_store=agent_store,
    )
    task = agent_store.list_agent_tasks()[0]
    assert task.target_agent == CODEX_M2
    assert task.execution_evidence["_routing"]["target_agent_explicit"] is False

    decision = run(
        schedule_engineering_workforce(
            task,
            scheduler_settings(),
            signal_client=FakeWorkforceAgentBusClient(status={"status": "busy", "availability": "busy"}),
        )
    )

    assert decision.applied is True
    assert decision.scheduler_mode is True
    assert decision.selected_agent == CIRCUIT_FORGE
    assert decision.target_agent_explicit is False


def test_workflow_explicit_alias_is_canonicalized_at_creation() -> None:
    agent_store = InMemoryAgentTaskStore()
    workflow_store = InMemoryWorkflowStore()

    create_workflow(
        WorkflowCreateRequest(
            repo_full_name="marcus937/riseos-agent-orchestrator",
            title="Alias workflow",
            tasks=[WorkflowTask(task_key="WF1", title="Circuit task", objective="Implement a backend endpoint.", target_agent="Circuit Forge")],
        ),
        workflow_store=workflow_store,
        agent_task_store=agent_store,
    )

    task = agent_store.list_agent_tasks()[0]
    assert task.target_agent == CIRCUIT_FORGE
    assert task.execution_evidence["_routing"]["target_agent_explicit"] is True
    assert task.execution_evidence["_routing"]["original_target_agent"] == "Circuit Forge"
    assert task.execution_evidence["_routing"]["canonical_target_agent"] == CIRCUIT_FORGE


def test_blocked_tasks_are_not_scheduled() -> None:
    agent_store = InMemoryAgentTaskStore()
    workflow_store = InMemoryWorkflowStore()
    create_workflow(
        WorkflowCreateRequest(
            repo_full_name="marcus937/riseos-agent-orchestrator",
            title="Dependency workflow",
            tasks=[
                WorkflowTask(task_key="WF1", title="First task", objective="Implement a backend endpoint.", target_agent=CODEX_M2),
                WorkflowTask(task_key="WF2", title="Second task", objective="Implement another backend endpoint.", depends_on=["WF1"]),
            ],
        ),
        workflow_store=workflow_store,
        agent_task_store=agent_store,
    )
    bus = FakeWorkforceAgentBusClient(status={"status": "busy", "availability": "busy"})

    released = run(release_runnable_agent_tasks(agent_store, bus, settings=scheduler_settings()))

    blocked_task = next(task for task in agent_store.list_agent_tasks() if task.title == "Second task")
    assert len(released) == 1
    assert blocked_task.blocked is True
    assert SCHEDULER_METADATA_KEY not in blocked_task.execution_evidence
    assert all(payload["metadata"]["task_id"] != blocked_task.task_id for payload in bus.payloads)


def test_unknown_codex_status_does_not_reroute_to_circuit() -> None:
    task = make_task(target_agent="auto-engineer")
    bus = FakeWorkforceAgentBusClient(status_error=RuntimeError("status unavailable"))

    decision = run(schedule_engineering_workforce(task, scheduler_settings(), signal_client=bus))
    apply_scheduler_decision(task, decision)

    assert decision.applied is True
    assert decision.selected_agent == CODEX_M2
    assert decision.reason == "codex_unknown_preserved"
    assert decision.candidates[0].availability_state == "unknown"
    assert task.target_agent == CODEX_M2


def test_scheduler_prefers_codex_when_available() -> None:
    task = make_task(target_agent="auto-engineer")

    decision = run(schedule_engineering_workforce(task, scheduler_settings(), signal_client=FakeWorkforceAgentBusClient()))
    apply_scheduler_decision(task, decision)

    assert decision.applied is True
    assert decision.selected_agent == CODEX_M2
    assert decision.reason == "preferred_available"
    assert task.target_agent == CODEX_M2


def test_scheduler_falls_back_to_circuit_when_codex_busy_for_backend_work() -> None:
    task = make_task(target_agent="coding")
    task.execution_evidence = {"repo_profile": "backend"}
    bus = FakeWorkforceAgentBusClient(status={"status": "busy", "health_state": "healthy", "availability": "busy"})

    decision = run(schedule_engineering_workforce(task, scheduler_settings(), signal_client=bus))
    apply_scheduler_decision(task, decision)

    assert decision.applied is True
    assert decision.selected_agent == CIRCUIT_FORGE
    assert decision.reason == "codex_busy"
    assert task.target_agent == CIRCUIT_FORGE


def test_scheduler_uses_circuit_when_metadata_prefers_circuit() -> None:
    task = make_task(target_agent="engineering")
    task.execution_evidence = {"preferred_engineering_worker": "circuit"}

    decision = run(schedule_engineering_workforce(task, scheduler_settings(), signal_client=FakeWorkforceAgentBusClient()))

    assert decision.applied is True
    assert decision.selected_agent == CIRCUIT_FORGE
    assert decision.reason == "metadata_preferred_circuit-forge"


def test_scheduler_does_not_route_frontend_tasks_to_circuit_by_default() -> None:
    task = make_task(target_agent="engineering", objective="Build a React frontend dashboard.")
    bus = FakeWorkforceAgentBusClient(status={"status": "busy", "health_state": "healthy", "availability": "busy"})

    decision = run(schedule_engineering_workforce(task, scheduler_settings(), signal_client=bus))
    apply_scheduler_decision(task, decision)

    assert decision.applied is True
    assert decision.selected_agent == CODEX_M2
    assert decision.reason == "frontend_requires_codex"
    assert task.target_agent == CODEX_M2


def test_frontend_repo_profile_blocks_circuit_fallback() -> None:
    task = make_task(target_agent="engineering", objective="Implement a reusable component.")
    task.execution_evidence = {"repo_profile": "frontend"}
    bus = FakeWorkforceAgentBusClient(status={"status": "busy", "health_state": "healthy", "availability": "busy"})

    decision = run(schedule_engineering_workforce(task, scheduler_settings(), signal_client=bus))

    assert decision.applied is True
    assert decision.selected_agent == CODEX_M2
    assert decision.reason == "frontend_requires_codex"
    assert decision.candidates[1].reason == "frontend_repo_not_allowed_for_circuit"


def test_documentation_tasks_can_fallback_to_circuit_when_codex_busy() -> None:
    task = make_task(target_agent="engineering", objective="Update README documentation for the backend service.")
    bus = FakeWorkforceAgentBusClient(status={"status": "busy", "health_state": "healthy", "availability": "busy"})

    decision = run(schedule_engineering_workforce(task, scheduler_settings(), signal_client=bus))

    assert decision.applied is True
    assert decision.selected_agent == CIRCUIT_FORGE
    assert decision.reason == "codex_busy"


def test_scheduler_records_metadata_on_task_and_agent_bus_payload() -> None:
    store = InMemoryAgentTaskStore()
    task = make_task(target_agent="coding")
    store.save_agent_task(task)
    bus = FakeWorkforceAgentBusClient(status={"status": "busy", "health_state": "healthy", "availability": "busy"})

    released = run(release_runnable_agent_tasks(store, bus, settings=scheduler_settings()))

    assert len(released) == 1
    payload = bus.payloads[0]
    assert payload["owner_agent"] == CIRCUIT_FORGE
    assert payload["metadata"]["scheduler_mode"] is True
    assert payload["metadata"]["scheduler_selected_agent"] == CIRCUIT_FORGE
    assert payload["metadata"]["scheduler_reason"] == "codex_busy"
    assert payload["metadata"]["original_target_agent"] == "coding"
    assert payload["metadata"]["target_agent_explicit"] is True
    assert [candidate["agent_id"] for candidate in payload["metadata"]["scheduler_candidates"]] == [CODEX_M2, CIRCUIT_FORGE]

    saved = store.get_agent_task(task.task_id)
    assert saved is not None
    scheduler_metadata = saved.execution_evidence[SCHEDULER_METADATA_KEY]
    assert scheduler_metadata["scheduler_selected_agent"] == CIRCUIT_FORGE
    assert any(event.event == "engineering_workforce_scheduled" for event in saved.lifecycle_events)


def test_release_preserves_explicit_target_agent_payloads_with_scheduler_enabled() -> None:
    for target_agent in (CODEX_M2, CIRCUIT_FORGE):
        store = InMemoryAgentTaskStore()
        task = make_task(target_agent=target_agent)
        store.save_agent_task(task)
        bus = FakeWorkforceAgentBusClient(status={"status": "busy", "health_state": "healthy", "availability": "busy"})

        released = run(release_runnable_agent_tasks(store, bus, settings=scheduler_settings()))

        assert len(released) == 1
        assert bus.payloads[0]["owner_agent"] == target_agent
        assert "scheduler_selected_agent" not in bus.payloads[0]["metadata"]
