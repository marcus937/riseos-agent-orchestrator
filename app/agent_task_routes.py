from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.admin_auth import require_orchestrator_admin_token
from app.agent_task_dispatch import AgentTaskDependencyBlocked, dispatch_agent_task_to_agent_bus
from app.agent_task_release import dispatch_circuit_wakeup_for_assigned_task, release_runnable_agent_tasks
from app.agent_tasks import (
    AgentTask,
    AgentTaskCreateRequest,
    AgentTaskCreateResponse,
    AgentTaskExecutionResult,
    AgentTaskStore,
    apply_execution_result,
    build_agent_task_store,
    create_agent_task,
    mark_agent_task_assigned,
    mark_agent_task_dispatch_failed,
    missing_dependency_task_ids,
    refresh_agent_task_dependency_state,
    refresh_agent_task_dependency_states,
)
from app.clients.agent_bus import AgentBusClient
from app.clients.github import GitHubClient
from app.config import Settings, get_settings
from app.repository_discovery import (
    RepositoryRegistryStore,
    build_repository_registry,
    ensure_orchestration_enabled_repository,
)
from app.review_dispatch import dispatch_bb2_review_request_from_execution_result
from app.workflow_orchestration import build_workflow_store, update_shared_workflow_routing_after_result

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/agent-tasks", tags=["agent-tasks"])


@router.post("")
async def create_agent_task_endpoint(
    payload: dict[str, Any],
    request: Request,
    _: None = Depends(require_orchestrator_admin_token),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any] | AgentTaskCreateResponse:
    if _is_registration_only_payload(payload):
        return _register_agent_task_repository(payload, request, settings)

    task_request = AgentTaskCreateRequest.model_validate(payload)
    _require_orchestration_enabled_repository(task_request.repo_full_name, request, settings)
    store = _agent_task_store(request, settings)
    missing_dependencies = missing_dependency_task_ids(task_request.dependency_task_ids, store)
    if missing_dependencies:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"dependency_task_ids": missing_dependencies})

    existing_tasks = store.list_agent_tasks()
    task = create_agent_task(task_request)
    refresh_agent_task_dependency_state(task, {existing.task_id: existing for existing in existing_tasks})
    store.save_agent_task(task)

    if settings.enable_agent_bus_dispatch and not task.blocked:
        client, should_close = _agent_bus_client(request, settings)
        github_client = _github_dependency_client(settings)
        try:
            work_item_id = await dispatch_agent_task_to_agent_bus(task, client, review_agent=settings.agent_bus_review_agent, dependency_client=github_client)
        except AgentTaskDependencyBlocked as exc:
            task.agent_bus_dispatch_error = str(exc)
            store.save_agent_task(task)
        except Exception as exc:
            mark_agent_task_dispatch_failed(task, str(exc))
            store.save_agent_task(task)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Agent Bus dispatch failed: {exc}") from exc
        else:
            mark_agent_task_assigned(task, work_item_id=work_item_id)
            store.save_agent_task(task)
            await dispatch_circuit_wakeup_for_assigned_task(task, settings=settings, agent_bus_client=client)
            store.save_agent_task(task)
        finally:
            if github_client is not None:
                await github_client.aclose()
            if should_close:
                await client.aclose()

    return _agent_task_create_response(task)


@router.get("", response_model=list[AgentTask])
async def list_agent_tasks(request: Request, _: None = Depends(require_orchestrator_admin_token), settings: Settings = Depends(get_settings)) -> list[AgentTask]:
    store = _agent_task_store(request, settings)
    return _refresh_all_agent_tasks(store)


@router.get("/{task_id}", response_model=AgentTask)
async def get_agent_task(task_id: str, request: Request, _: None = Depends(require_orchestrator_admin_token), settings: Settings = Depends(get_settings)) -> AgentTask:
    store = _agent_task_store(request, settings)
    _refresh_all_agent_tasks(store)
    task = store.get_agent_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent task not found")
    return task


@router.post("/{task_id}/execution-result", response_model=AgentTask)
async def record_agent_task_execution_result(
    task_id: str,
    payload: AgentTaskExecutionResult,
    request: Request,
    _: None = Depends(require_orchestrator_admin_token),
    settings: Settings = Depends(get_settings),
) -> AgentTask:
    logger.info("execution-result payload=%s", json.dumps(payload.model_dump(mode="json"), default=str))
    store = _agent_task_store(request, settings)
    task = store.get_agent_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent task not found")
    if payload.agent_id != task.target_agent:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Execution result agent_id does not match the task target_agent.")
    apply_execution_result(task, payload)
    store.save_agent_task(task)
    _refresh_all_agent_tasks(store)
    _propagate_shared_workflow_routing(task, request, settings, store)

    if settings.enable_agent_bus_dispatch:
        client, should_close = _agent_bus_client(request, settings)
        github_client = _github_dependency_client(settings)
        try:
            await dispatch_bb2_review_request_from_execution_result(
                task,
                payload,
                client,
                review_agent=settings.agent_bus_review_agent,
                store=store,
            )
            await release_runnable_agent_tasks(
                store,
                client,
                review_agent=settings.agent_bus_review_agent,
                dependency_client=github_client,
                settings=settings,
            )
        finally:
            if github_client is not None:
                await github_client.aclose()
            if should_close:
                await client.aclose()

    refreshed = store.get_agent_task(task_id)
    return refreshed or task


def _agent_task_create_response(task: AgentTask) -> AgentTaskCreateResponse:
    return AgentTaskCreateResponse(task_id=task.task_id, status=task.status, created_at=task.created_at, target_agent=task.target_agent, dependency_task_ids=task.dependency_task_ids, blocked=task.blocked, blocked_by=task.blocked_by)


def _refresh_all_agent_tasks(store: AgentTaskStore) -> list[AgentTask]:
    tasks = refresh_agent_task_dependency_states(store.list_agent_tasks())
    for task in tasks:
        store.save_agent_task(task)
    return tasks


def _propagate_shared_workflow_routing(task: AgentTask, request: Request, settings: Settings, store: AgentTaskStore) -> None:
    if not task.correlation_id or not task.correlation_id.startswith("wf-"):
        return
    workflow_store = build_workflow_store(settings.orchestrator_db_path)
    workflow = workflow_store.get_workflow(task.correlation_id)
    if workflow is None:
        return
    update_shared_workflow_routing_after_result(workflow, store)
    workflow_store.save_workflow(workflow)


def _agent_task_store(request: Request, settings: Settings) -> AgentTaskStore:
    store = getattr(request.app.state, "agent_task_store", None)
    if store is None:
        store = build_agent_task_store(settings.orchestrator_db_path)
        request.app.state.agent_task_store = store
    return store


def _agent_bus_client(request: Request, settings: Settings) -> tuple[AgentBusClient, bool]:
    client = getattr(request.app.state, "agent_bus_client", None)
    if client is not None:
        return client, False
    return (AgentBusClient(base_url=settings.agent_bus_base_url, token=settings.agent_bus_token, timeout_seconds=settings.agent_bus_timeout_seconds), True)


def _github_dependency_client(settings: Settings) -> GitHubClient | None:
    if not settings.github_token:
        return None
    return GitHubClient(token=settings.github_token)


def _repository_registry(request: Request, settings: Settings) -> RepositoryRegistryStore:
    registry = getattr(request.app.state, "repository_registry", None)
    if registry is None:
        registry = build_repository_registry(settings)
        request.app.state.repository_registry = registry
    return registry


def _require_orchestration_enabled_repository(repo_full_name: str, request: Request, settings: Settings) -> None:
    record = ensure_orchestration_enabled_repository(_repository_registry(request, settings), repo_full_name, trusted_owner=settings.trusted_repository_owner)
    if record is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Repository is not orchestration-enabled.")
    if record.archived:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Repository is archived.")
    if not record.orchestration_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Repository is not orchestration-enabled.")


def _is_registration_only_payload(payload: dict[str, Any]) -> bool:
    return not any(payload.get(key) for key in ("objective", "body", "instructions", "acceptance_criteria", "target_agent", "priority", "correlation_id", "dependency_task_ids"))


def _register_agent_task_repository(payload: dict[str, Any], request: Request, settings: Settings) -> dict[str, Any]:
    repo_full_name = str(payload.get("repo_full_name") or "")
    if not repo_full_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="repo_full_name is required.")
    registry = _repository_registry(request, settings)
    existed_before = registry.get_repository_registry_record(repo_full_name) is not None
    record = ensure_orchestration_enabled_repository(
        registry,
        repo_full_name,
        trusted_owner=settings.trusted_repository_owner,
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Repository is not orchestration-enabled.")
    if record.archived:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Repository is archived.")
    if not record.orchestration_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Repository is not orchestration-enabled.")
    return {
        "accepted": True,
        "repo_full_name": record.repo_full_name,
        "orchestration_enabled": True,
        "auto_registered": not existed_before,
        "issue_number": payload.get("issue_number"),
    }
