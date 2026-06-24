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
    workflow_id = task.correlation_id if task.correlation_id and task.correlation_id.startswith("wf-") else f"wf-agent-task-{task.task_id}"
    dependency_state = dependency_state or DependencyState()
    routing = _routing_metadata(task)
    metadata = {
        "task_id": task.task_id,
        "workflow_id": workflow_id,
        "correlation_id": task.correlation_id,
        "repo_full_name": task.repo_full_name,
        "objective": task.objective,
        "instructions": task.instructions,
        "acceptance_criteria": task.acceptance_criteria,
        "target_agent": task.target_agent,
        "dependency_task_ids": task.dependency_task_ids,
        "dependency_count": dependency_state.dependency_count,
        "dependencies_satisfied": dependency_state.dependencies_satisfied,
        "blocked": not dependency_state.dependencies_satisfied,
        "blocked_by": dependency_state.blocked_by,
        "source": "riseos-agent-orchestrator.agent_task",
        "callback": {
            "method": "POST",
            "path": f"/api/v1/agent-tasks/{task.task_id}/execution-result",
        },
    }
    metadata.update(routing)
    payload: dict[str, Any] = {
        "title": task.title,
        "repository": task.repo_full_name,
        "issue_number": task.issue_number,
        "priority": task.priority.value,
        "owner_agent": task.target_agent,
        "review_agent": review_agent,
        "metadata": metadata,
    }
    if routing.get("source_pr_number") is not None:
        payload["pr_number"] = routing["source_pr_number"]
    if routing.get("source_branch") is not None:
        payload["branch"] = routing["source_branch"]
    return payload


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
    response = await client.create_work_item(build_agent_bus_work_item_payload(task, review_agent=review_agent, dependency_state=dependency_state))
    raw_work_item_id = response.get("work_item_id")
    if not raw_work_item_id:
        raise AgentTaskDispatchError("Agent Bus work item response did not include work_item_id.")
    return str(raw_work_item_id)


async def evaluate_agent_task_dependencies(task: AgentTask, dependency_client: AgentTaskDependencyClient | None) -> DependencyState:
    if task.dependency_task_ids:
        return DependencyState(dependency_count=len(task.dependency_task_ids), dependencies_satisfied=not task.blocked, blocked_by=task.blocked_by)
    dependencies = parse_issue_dependencies(task.objective)
    if not dependencies.predecessor_issue_ids:
        return DependencyState()
    if dependency_client is None:
        return DependencyState(dependency_count=len(dependencies.predecessor_issue_ids), dependencies_satisfied=False, blocked_by=dependencies.predecessor_issue_ids)
    return await dependency_state_for_issue(task.repo_full_name, task.issue_number or 0, task.objective, dependency_client)


def _routing_metadata(task: AgentTask) -> dict[str, Any]:
    evidence = task.execution_evidence if isinstance(task.execution_evidence, dict) else {}
    routing = evidence.get("_routing") if isinstance(evidence.get("_routing"), dict) else {}
    allowed = {"pr_strategy", "base_branch", "source_branch", "source_pr_number", "rework_of_task_id", "rework_attempt", "review_decision_id"}
    return {key: value for key, value in routing.items() if key in allowed and value is not None}
