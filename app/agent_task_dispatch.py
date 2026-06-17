from __future__ import annotations

from typing import Any, Protocol

from app.agent_tasks import AgentTask


class AgentBusDispatchClient(Protocol):
    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


class AgentTaskDispatchError(Exception):
    """Raised when an AgentTask cannot be dispatched to Agent Bus."""


def build_agent_bus_work_item_payload(
    task: AgentTask,
    *,
    review_agent: str = "bb2",
) -> dict[str, Any]:
    workflow_id = f"wf-agent-task-{task.task_id}"
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
) -> str:
    response = await client.create_work_item(build_agent_bus_work_item_payload(task, review_agent=review_agent))
    raw_work_item_id = response.get("work_item_id") or response.get("id")
    if not raw_work_item_id:
        raise AgentTaskDispatchError("Agent Bus work item response did not include work_item_id.")
    return str(raw_work_item_id)
