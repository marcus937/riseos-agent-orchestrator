from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.admin_auth import require_orchestrator_admin_token
from app.agent_task_dispatch import dispatch_agent_task_to_agent_bus
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
)
from app.clients.agent_bus import AgentBusClient
from app.config import Settings, get_settings
from app.repository_discovery import RepositoryRegistryStore, build_repository_registry

router = APIRouter(prefix="/api/v1/agent-tasks", tags=["agent-tasks"])


@router.post("", response_model=AgentTaskCreateResponse)
async def create_agent_task_endpoint(
    payload: AgentTaskCreateRequest,
    request: Request,
    _: None = Depends(require_orchestrator_admin_token),
    settings: Settings = Depends(get_settings),
) -> AgentTaskCreateResponse:
    _require_orchestration_enabled_repository(payload.repo_full_name, request, settings)
    store = _agent_task_store(request, settings)
    task = create_agent_task(payload)
    store.save_agent_task(task)

    if settings.enable_agent_bus_dispatch:
        client, should_close = _agent_bus_client(request, settings)
        try:
            work_item_id = await dispatch_agent_task_to_agent_bus(
                task,
                client,
                review_agent=settings.agent_bus_review_agent,
            )
        except Exception as exc:
            mark_agent_task_dispatch_failed(task, str(exc))
            store.save_agent_task(task)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Agent Bus dispatch failed: {exc}",
            ) from exc
        finally:
            if should_close:
                await client.aclose()
        mark_agent_task_assigned(task, work_item_id=work_item_id)
        store.save_agent_task(task)

    return AgentTaskCreateResponse(
        task_id=task.task_id,
        status=task.status,
        created_at=task.created_at,
        target_agent=task.target_agent,
    )


@router.get("", response_model=list[AgentTask])
async def list_agent_tasks(
    request: Request,
    _: None = Depends(require_orchestrator_admin_token),
    settings: Settings = Depends(get_settings),
) -> list[AgentTask]:
    return _agent_task_store(request, settings).list_agent_tasks()


@router.get("/{task_id}", response_model=AgentTask)
async def get_agent_task(
    task_id: str,
    request: Request,
    _: None = Depends(require_orchestrator_admin_token),
    settings: Settings = Depends(get_settings),
) -> AgentTask:
    task = _agent_task_store(request, settings).get_agent_task(task_id)
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
    store = _agent_task_store(request, settings)
    task = store.get_agent_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent task not found")
    if payload.agent_id != task.target_agent:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Execution result agent_id does not match the task target_agent.",
        )
    apply_execution_result(task, payload)
    store.save_agent_task(task)
    return task


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
    return (
        AgentBusClient(
            base_url=settings.agent_bus_base_url,
            token=settings.agent_bus_token,
            timeout_seconds=settings.agent_bus_timeout_seconds,
        ),
        True,
    )


def _repository_registry(request: Request, settings: Settings) -> RepositoryRegistryStore:
    registry = getattr(request.app.state, "repository_registry", None)
    if registry is None:
        registry = build_repository_registry(settings)
        request.app.state.repository_registry = registry
    return registry


def _require_orchestration_enabled_repository(repo_full_name: str, request: Request, settings: Settings) -> None:
    record = _repository_registry(request, settings).get_repository_registry_record(repo_full_name)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Repository is not orchestration-enabled.",
        )
    if record.archived or not record.orchestration_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Repository is not orchestration-enabled.",
        )
