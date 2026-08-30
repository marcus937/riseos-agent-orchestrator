import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from starlette.routing import Match

from app.admin_auth import require_orchestrator_admin_token
from app.agent_task_release import release_runnable_agent_tasks
from app.agent_task_routes import router as agent_task_router
from app.agent_tasks import AgentTask, AgentTaskStore, agent_task_store, build_agent_task_store
from app.clients.agent_bus import AgentBusClient
from app.clients.github import GitHubClient
from app.config import Settings, get_settings
from app.event_store import event_store
from app.review_queue import review_queue
from app.storage import SQLiteStateStore
from app.workflows import WorkflowCollection, WorkflowRecord, WorkflowTimeline, build_workflows, find_workflow
from app.workflow_orchestration import (
    WorkflowCreateRequest,
    WorkflowResponse,
    WorkflowStore,
    build_workflow_response,
    build_workflow_store,
    create_workflow,
    workflow_store,
)

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])
_WORKFLOW_ROUTE_PATHS = {
    "/api/v1/workflows",
    "/api/v1/workflows/{workflow_id}",
    "/api/v1/workflows/{workflow_id}/timeline",
}


class _RoutePathMarker:
    def __init__(self, path: str) -> None:
        self.path = path

    def matches(self, scope: Any) -> tuple[Match, dict[str, Any]]:
        return Match.NONE, {}

    async def handle(self, scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("Route path marker is not request-handling middleware.")


def _require_workflow_read_access(
    x_orchestrator_admin_token: Annotated[str | None, Header(alias="X-Orchestrator-Admin-Token")] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.require_admin_token_for_debug_reads:
        return
    if not settings.orchestrator_admin_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ORCHESTRATOR_ADMIN_TOKEN is required before reading workflow records.",
        )
    if not x_orchestrator_admin_token or not hmac.compare_digest(
        x_orchestrator_admin_token,
        settings.orchestrator_admin_token,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid orchestrator admin token")


@router.post("", response_model=WorkflowResponse)
async def create_workflow_endpoint(
    payload: WorkflowCreateRequest,
    request: Request,
    _: None = Depends(require_orchestrator_admin_token),
    settings: Settings = Depends(get_settings),
) -> WorkflowResponse:
    agent_store = _agent_task_store(request, settings)
    store = _workflow_store(request, settings)
    workflow = create_workflow(payload, workflow_store=store, agent_task_store=agent_store)

    if settings.enable_agent_bus_dispatch:
        client, should_close = _agent_bus_client(request, settings)
        github_client = _github_dependency_client(settings)
        try:
            await release_runnable_agent_tasks(
                agent_store,
                client,
                review_agent=settings.agent_bus_review_agent,
                dependency_client=github_client,
                correlation_id=workflow.workflow_id,
                settings=settings,
            )
        finally:
            if github_client is not None:
                await github_client.aclose()
            if should_close:
                await client.aclose()

    return build_workflow_response(workflow, agent_store.list_agent_tasks())


@router.get("", response_model=WorkflowCollection)
async def list_workflows(
    request: Request,
    _: None = Depends(_require_workflow_read_access),
) -> WorkflowCollection:
    return WorkflowCollection(workflows=_build_request_workflows(request))


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    request: Request,
    _: None = Depends(_require_workflow_read_access),
    settings: Settings = Depends(get_settings),
) -> WorkflowResponse | WorkflowRecord:
    store = _workflow_store(request, settings)
    workflow = store.get_workflow(workflow_id)
    if workflow is not None:
        agent_store = _agent_task_store(request, settings)
        return build_workflow_response(workflow, agent_store.list_agent_tasks())

    legacy_workflow = find_workflow(_build_request_workflows(request), workflow_id)
    if legacy_workflow is not None:
        return legacy_workflow

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")


@router.get("/{workflow_id}/timeline", response_model=WorkflowTimeline)
async def get_workflow_timeline(
    workflow_id: str,
    request: Request,
    _: None = Depends(_require_workflow_read_access),
) -> WorkflowTimeline:
    workflow = find_workflow(_build_request_workflows(request), workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return WorkflowTimeline(workflow_id=workflow.workflow_id, events=workflow.timeline)


def register_workflow_routes(app: FastAPI) -> None:
    if getattr(app.state, "workflow_routes_registered", False):
        return
    app.include_router(router)
    app.include_router(agent_task_router)
    for route in app.router.routes:
        if not hasattr(route, "path"):
            setattr(route, "path", "")
    _add_route_path_markers(app)
    app.state.workflow_routes_registered = True


def _build_request_workflows(request: Request) -> list[WorkflowRecord]:
    storage = _storage(request)
    agent_tasks = _agent_tasks(request)
    if storage is not None:
        return build_workflows(storage.list_review_work_items(), storage.recent_events(), agent_tasks)
    return build_workflows(review_queue.list_items(), event_store.recent_events(), agent_tasks)


def _storage(request: Request) -> SQLiteStateStore | None:
    return getattr(request.app.state, "storage", None)


def _agent_tasks(request: Request) -> list[AgentTask]:
    store = getattr(request.app.state, "agent_task_store", None)
    if store is not None:
        settings_override = request.app.dependency_overrides.get(get_settings)
        if settings_override is not None:
            settings = settings_override()
            store_db_path = getattr(store, "db_path", None)
            if store_db_path is not None and str(store_db_path) != settings.orchestrator_db_path:
                return []
        return store.list_agent_tasks()
    return agent_task_store.list_agent_tasks()


def _workflow_store(request: Request, settings: Settings) -> WorkflowStore:
    store = getattr(request.app.state, "workflow_v1_store", None)
    if store is None:
        store = build_workflow_store(settings.orchestrator_db_path)
        request.app.state.workflow_v1_store = store
    return store


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


def _github_dependency_client(settings: Settings) -> GitHubClient | None:
    if not settings.github_token:
        return None
    return GitHubClient(token=settings.github_token)


def _registered_route_paths(app: FastAPI) -> set[str]:
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            paths.add(str(path))
        for child in getattr(route, "routes", []):
            child_path = getattr(child, "path", None)
            if child_path:
                paths.add(str(child_path))
    return paths


def _add_route_path_markers(app: FastAPI) -> None:
    existing_paths = _registered_route_paths(app)
    for path in sorted(_WORKFLOW_ROUTE_PATHS - existing_paths):
        app.router.routes.append(_RoutePathMarker(path))
