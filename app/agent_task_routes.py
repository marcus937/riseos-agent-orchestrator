from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.agent_tasks import (
    AgentTask,
    AgentTaskCreateRequest,
    AgentTaskCreateResponse,
    AgentTaskStore,
    build_agent_task_store,
    create_agent_task,
)
from app.config import Settings, get_settings
from app.repository_discovery import RepositoryRegistryStore, build_repository_registry

router = APIRouter(prefix="/api/v1/agent-tasks", tags=["agent-tasks"])


@router.post("", response_model=AgentTaskCreateResponse)
async def create_agent_task_endpoint(
    payload: AgentTaskCreateRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> AgentTaskCreateResponse:
    _require_orchestration_enabled_repository(payload.repo_full_name, request, settings)
    task = create_agent_task(payload)
    _agent_task_store(request, settings).save_agent_task(task)
    return AgentTaskCreateResponse(
        task_id=task.task_id,
        status=task.status,
        created_at=task.created_at,
        target_agent=task.target_agent,
    )


@router.get("", response_model=list[AgentTask])
async def list_agent_tasks(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> list[AgentTask]:
    return _agent_task_store(request, settings).list_agent_tasks()


@router.get("/{task_id}", response_model=AgentTask)
async def get_agent_task(
    task_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> AgentTask:
    task = _agent_task_store(request, settings).get_agent_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent task not found")
    return task


def _agent_task_store(request: Request, settings: Settings) -> AgentTaskStore:
    store = getattr(request.app.state, "agent_task_store", None)
    if store is None:
        store = build_agent_task_store(settings.orchestrator_db_path)
        request.app.state.agent_task_store = store
    return store


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
