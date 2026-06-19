import asyncio
from typing import Any

import pytest

from app.agent_task_dispatch import AgentTaskDependencyBlocked, dispatch_agent_task_to_agent_bus
from app.agent_tasks import AgentTaskCreateRequest, create_agent_task


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeAgentBusClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"work_item_id": "work-1"}


class FakeDependencyClient:
    def __init__(self, issues: dict[int, dict[str, Any]]) -> None:
        self.issues = issues

    async def fetch_issue(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        return self.issues[issue_number]


def task(objective: str, *, target_agent: str = "codex-m2"):
    return create_agent_task(
        AgentTaskCreateRequest(
            repo_full_name="riseos/example",
            title="Direct agent task",
            objective=objective,
            target_agent=target_agent,
        )
    )


def test_direct_agent_task_without_dependencies_dispatches_to_agent_bus() -> None:
    agent_bus = FakeAgentBusClient()

    work_item_id = run(dispatch_agent_task_to_agent_bus(task("Do the thing."), agent_bus))

    assert work_item_id == "work-1"
    assert len(agent_bus.payloads) == 1
    assert agent_bus.payloads[0]["metadata"]["dependencies_satisfied"] is True


def test_direct_agent_task_blocks_agent_bus_when_dependency_incomplete() -> None:
    agent_bus = FakeAgentBusClient()
    dependency_client = FakeDependencyClient({72: {"state": "open", "labels": []}})

    with pytest.raises(AgentTaskDependencyBlocked) as exc_info:
        run(dispatch_agent_task_to_agent_bus(task("depends_on:\n  - issue:72"), agent_bus, dependency_client=dependency_client))

    assert exc_info.value.dependency_state.blocked_by == [72]
    assert agent_bus.payloads == []


def test_direct_agent_task_dispatches_after_dependency_completion() -> None:
    agent_bus = FakeAgentBusClient()
    dependency_client = FakeDependencyClient(
        {72: {"state": "open", "labels": [{"name": "bb2-approved"}, {"name": "ready-to-merge"}]}}
    )

    work_item_id = run(dispatch_agent_task_to_agent_bus(task("predecessor_issue: 72", target_agent="hermes-runtime"), agent_bus, dependency_client=dependency_client))

    assert work_item_id == "work-1"
    payload = agent_bus.payloads[0]
    assert payload["owner_agent"] == "hermes-runtime"
    assert payload["metadata"]["dependency_count"] == 1
    assert payload["metadata"]["dependencies_satisfied"] is True
    assert payload["metadata"]["blocked_by"] == []
