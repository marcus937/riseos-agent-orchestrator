from __future__ import annotations

from app.agent_task_dispatch import AgentTaskDependencyBlocked, dispatch_agent_task_to_agent_bus
from app.agent_tasks import (
    AgentTask,
    AgentTaskStatus,
    AgentTaskStore,
    mark_agent_task_assigned,
    mark_agent_task_dispatch_failed,
    refresh_agent_task_dependency_states,
)


async def release_runnable_agent_tasks(
    store: AgentTaskStore,
    client: object,
    *,
    review_agent: str = "bb2",
    dependency_client: object | None = None,
) -> list[AgentTask]:
    tasks = refresh_agent_task_dependency_states(store.list_agent_tasks())
    tasks_by_id = {task.task_id: task for task in tasks}
    released: list[AgentTask] = []

    for task in tasks:
        store.save_agent_task(task)
        if not _is_runnable(task):
            continue
        try:
            work_item_id = await dispatch_agent_task_to_agent_bus(
                task,
                client,
                review_agent=review_agent,
                dependency_client=dependency_client,
            )
        except AgentTaskDependencyBlocked as exc:
            task.agent_bus_dispatch_error = str(exc)
            store.save_agent_task(task)
            continue
        except Exception as exc:
            mark_agent_task_dispatch_failed(task, str(exc))
            store.save_agent_task(task)
            released.append(task)
            continue
        mark_agent_task_assigned(task, work_item_id=work_item_id)
        store.save_agent_task(task)
        tasks_by_id[task.task_id] = task
        released.append(task)

    return released


def _is_runnable(task: AgentTask) -> bool:
    return (
        task.status == AgentTaskStatus.QUEUED
        and not task.blocked
        and not task.agent_bus_work_item_id
        and not task.agent_bus_dispatch_error
    )
