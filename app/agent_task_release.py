from __future__ import annotations

import logging

from app.agent_task_dispatch import AgentTaskDependencyBlocked, dispatch_agent_task_to_agent_bus
from app.agent_tasks import (
    AgentTask,
    AgentTaskStatus,
    AgentTaskStore,
    mark_agent_task_assigned,
    mark_agent_task_dispatch_failed,
    refresh_agent_task_dependency_states,
)
from app.circuit_agent_trigger import is_circuit_agent, wake_circuit_agent_for_work
from app.config import Settings


logger = logging.getLogger("riseos_agent_orchestrator")


async def release_runnable_agent_tasks(
    store: AgentTaskStore,
    client: object,
    *,
    review_agent: str = "bb2",
    dependency_client: object | None = None,
    correlation_id: str | None = None,
    settings: Settings | None = None,
) -> list[AgentTask]:
    tasks = refresh_agent_task_dependency_states(store.list_agent_tasks())
    tasks_by_id = {task.task_id: task for task in tasks}
    released: list[AgentTask] = []

    for task in tasks:
        store.save_agent_task(task)
        if correlation_id is not None and task.correlation_id != correlation_id:
            continue
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
        await dispatch_circuit_wakeup_for_assigned_task(task, settings=settings)
        tasks_by_id[task.task_id] = task
        released.append(task)

    return released


async def dispatch_circuit_wakeup_for_assigned_task(task: AgentTask, *, settings: Settings | None) -> None:
    if settings is None or not is_circuit_agent(task.target_agent):
        return

    logger.info(
        "[CIRCUIT] task assigned task_id=%s target_agent=%s workflow_id=%s work_item_id=%s",
        task.task_id,
        task.target_agent,
        task.correlation_id,
        task.agent_bus_work_item_id,
    )
    logger.info("[CIRCUIT] dispatch starting task_id=%s target_agent=%s", task.task_id, task.target_agent)
    logger.info("[CIRCUIT] trigger POST task_id=%s workflow_id=%s", task.task_id, task.correlation_id)
    try:
        result = await wake_circuit_agent_for_work(
            settings,
            target_agent=task.target_agent,
            repo_full_name=task.repo_full_name,
            issue_number=task.issue_number,
            workflow_id=task.correlation_id,
        )
    except Exception as exc:
        logger.warning("[CIRCUIT] dispatch complete task_id=%s success=false error_type=%s", task.task_id, type(exc).__name__)
        return

    logger.info(
        "[CIRCUIT] trigger response task_id=%s success=%s status_code=%s skipped_reason=%s error=%s",
        task.task_id,
        result.success,
        result.status_code,
        result.skipped_reason,
        result.error,
    )
    logger.info("[CIRCUIT] dispatch complete task_id=%s success=%s", task.task_id, result.success)


def _is_runnable(task: AgentTask) -> bool:
    return (
        task.status == AgentTaskStatus.QUEUED
        and not task.blocked
        and not task.agent_bus_work_item_id
        and not task.agent_bus_dispatch_error
    )
