from __future__ import annotations

from typing import Any, Protocol

from app.agent_tasks import AgentTask
from app.task_dependencies import DependencyState, dependency_state_for_issue, parse_issue_dependencies


class AgentBusDispatchClient(Protocol):
    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


class AgentTaskDependencyClient(Protocol):
    async def fetch_issue(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        ...


class AgentTaskDispatchError(Exception):
    """Raised when an AgentTask cannot be dispatched to Agent Bus."""


class AgentTaskDependencyBlocked(AgentTaskDispatchError):
    """Raised when an AgentTask is queued behind incomplete dependencies."""

    def __init__(self, dependency_state: DependencyState) -> None:
        self.dependency_state = dependency_state
        super().__init__(f"AgentTask dependencies are not satisfied; blocked_by={dependency_state.blocked_by}")


def build_agent_bus_work_item_payload(
    task: AgentTask,
    *,
    review_agent: str = "bb2",
    dependency_state: DependencyState | None = None,
) -> dict[str, Any]:
    workflow_id = f"wf-agent-task-{task.task_id}"
    dependency_state = dependency_state or DependencyState()
    return {
        "title": task.title,
        "repository": task.repo_full_name,
        "issue_number": task.issue_number,
        "priority": task.priority.value,
        "owner_agent": task.target_agent,
        "review_agent": review_agent,
        "metadata": {
            "task_id": task.task_id,
            "workflow_id": workflow_id,
            "repo_full_name": task.repo_full_name,
            "objective": task.objective,
            "instructions": task.instructions,
            "acceptance_criteria": task.acceptance_criteria,
            "target_agent": task.target_agent,
            "dependency_count": dependency_state.dependency_count,
            "dependencies_satisfied": dependency_state.dependencies_satisfied,
            "blocked_by": dependency_state.blocked_by,
            "source": "riseos-agent-orchestrator.agent_task",
            "callback": {
                "method": "POST",
                "path": f"/api/v1/agent-tasks/{task.task_id}/execution-result",
            },
        },
    }


async def dispatch_agent_task_to_agent_bus(
    task: AgentTask,
    client: AgentBusDispatchClient,
    *,
    review_agent: str = "bb2",
    dependency_client: AgentTaskDependencyClient | None = None,
) -> str:
    dependency_state = await evaluate_agent_task_dependencies(task, dependency_client)
    if not dependency_state.dependencies_satisfied:
        raise AgentTaskDependencyBlocked(dependency_state)
    response = await client.create_work_item(
        build_agent_bus_work_item_payload(task, review_agent=review_agent, dependency_state=dependency_state)
    )
    raw_work_item_id = response.get("work_item_id")
    if not raw_work_item_id:
        raise AgentTaskDispatchError("Agent Bus work item response did not include work_item_id.")
    return str(raw_work_item_id)


async def evaluate_agent_task_dependencies(
    task: AgentTask,
    dependency_client: AgentTaskDependencyClient | None,
) -> DependencyState:
    dependencies = parse_issue_dependencies(task.objective)
    if not dependencies.predecessor_issue_ids:
        return DependencyState()
    if dependency_client is None:
        return DependencyState(
            dependency_count=len(dependencies.predecessor_issue_ids),
            dependencies_satisfied=False,
            blocked_by=dependencies.predecessor_issue_ids,
        )
    return await dependency_state_for_issue(
        task.repo_full_name,
        task.issue_number or 0,
        task.objective,
        dependency_client,
    )
