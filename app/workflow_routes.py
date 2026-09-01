import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, status
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
from app.workflows import (
    WORKFLOW_LIST_DEFAULT_LIMIT,
    WORKFLOW_LIST_DEFAULT_RECENT_DAYS,
    WORKFLOW_LIST_MAX_LIMIT,
    WORKFLOW_LIST_MAX_RECENT_DAYS,
    WorkflowCollection,
    WorkflowListFilter,
    WorkflowPaginationMetadata,
    WorkflowRecord,
    WorkflowTimeline,
    build_workflow_collection,
    build_workflows,
    find_workflow,
)
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
    limit: Annotated[int, Query(ge=1, le=WORKFLOW_LIST_MAX_LIMIT)] = WORKFLOW_LIST_DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
    workflow_filter: Annotated[WorkflowListFilter, Query(alias="filter")] = WorkflowListFilter.ACTIVE_RECENT,
    recent_days: Annotated[int, Query(ge=1, le=WORKFLOW_LIST_MAX_RECENT_DAYS)] = WORKFLOW_LIST_DEFAULT_RECENT_DAYS,
    _: None = Depends(_require_workflow_read_access),
    settings: Settings = Depends(get_settings),
) -> WorkflowCollection:
    return _build_request_workflow_collection(
        request,
        settings,
        limit=limit,
        offset=offset,
        workflow_filter=workflow_filter,
        recent_days=recent_days,
    )


def _build_request_workflow_collection(
    request: Request,
    settings: Settings,
    *,
    limit: int,
    offset: int,
    workflow_filter: WorkflowListFilter,
    recent_days: int,
) -> WorkflowCollection:
    storage = _storage(request)
    if storage is not None and _supports_bounded_workflow_storage(storage):
        agent_store = _agent_task_store_for_read(request, settings)
        if not _supports_bounded_agent_task_store(agent_store):
            return build_workflow_collection(
                _build_request_workflows(request),
                limit=limit,
                offset=offset,
                workflow_filter=workflow_filter,
                recent_days=recent_days,
            )
        return _build_bounded_storage_workflow_collection(
            storage,
            agent_store,
            limit=limit,
            offset=offset,
            workflow_filter=workflow_filter,
            recent_days=recent_days,
        )
    return build_workflow_collection(
        _build_request_workflows(request),
        limit=limit,
        offset=offset,
        workflow_filter=workflow_filter,
        recent_days=recent_days,
    )


def _build_bounded_storage_workflow_collection(
    storage: SQLiteStateStore,
    agent_store: AgentTaskStore | None,
    *,
    limit: int,
    offset: int,
    workflow_filter: WorkflowListFilter,
    recent_days: int,
) -> WorkflowCollection:
    bounded_limit = min(max(limit, 1), WORKFLOW_LIST_MAX_LIMIT)
    bounded_offset = max(offset, 0)
    bounded_recent_days = min(max(recent_days, 1), WORKFLOW_LIST_MAX_RECENT_DAYS)
    normalized_filter = WorkflowListFilter(workflow_filter)
    workflow_filter_value = normalized_filter.value
    now = datetime.now(UTC)
    recent_since = now - timedelta(days=bounded_recent_days)
    candidate_limit = bounded_offset + bounded_limit
    review_total = storage.count_review_work_items_for_workflow_collection(
        workflow_filter=workflow_filter_value,
        recent_since=recent_since,
    )
    agent_task_total = (
        agent_store.count_agent_tasks_for_workflow_collection(
            workflow_filter=workflow_filter_value,
            recent_since=recent_since,
        )
        if agent_store is not None
        else 0
    )
    event_total = storage.count_event_records_for_workflow_collection(
        workflow_filter=workflow_filter_value,
        recent_since=recent_since,
    )
    review_items = storage.list_review_work_items_for_workflow_collection(
        limit=candidate_limit,
        workflow_filter=workflow_filter_value,
        recent_since=recent_since,
    )
    agent_tasks = (
        agent_store.list_agent_tasks_for_workflow_collection(
            limit=candidate_limit,
            workflow_filter=workflow_filter_value,
            recent_since=recent_since,
        )
        if agent_store is not None
        else []
    )
    event_candidates = storage.list_event_records_for_workflow_collection(
        limit=candidate_limit,
        workflow_filter=workflow_filter_value,
        recent_since=recent_since,
    )
    total = review_total + agent_task_total + event_total
    unfiltered_total = (
        storage.count_review_work_items()
        + (agent_store.count_agent_tasks() if agent_store is not None else 0)
        + storage.count_event_records_for_workflow_collection(
            workflow_filter=WorkflowListFilter.ALL.value,
        )
    )
    workflows = build_workflows(review_items, event_candidates, agent_tasks)
    page = workflows[bounded_offset : bounded_offset + bounded_limit]
    next_offset = bounded_offset + bounded_limit if bounded_offset + bounded_limit < total else None
    return WorkflowCollection(
        workflows=page,
        pagination=WorkflowPaginationMetadata(
            limit=bounded_limit,
            offset=bounded_offset,
            returned=len(page),
            total=total,
            unfiltered_total=unfiltered_total,
            truncated=next_offset is not None,
            has_next=next_offset is not None,
            next_offset=next_offset,
            filter=normalized_filter,
            recent_days=bounded_recent_days,
        ),
    )


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


def _agent_task_store_for_read(request: Request, settings: Settings) -> AgentTaskStore | None:
    store = getattr(request.app.state, "agent_task_store", None)
    if store is not None:
        store_db_path = getattr(store, "db_path", None)
        if store_db_path is not None and str(store_db_path) != settings.orchestrator_db_path:
            return None
        return store
    return build_agent_task_store(settings.orchestrator_db_path)


def _supports_bounded_workflow_storage(storage: object) -> bool:
    return all(
        hasattr(storage, name)
        for name in (
            "count_event_records_for_workflow_collection",
            "count_review_work_items",
            "count_review_work_items_for_workflow_collection",
            "list_event_records_for_workflow_collection",
            "list_review_work_items_for_workflow_collection",
        )
    )


def _supports_bounded_agent_task_store(agent_store: object | None) -> bool:
    if agent_store is None:
        return True
    return all(
        hasattr(agent_store, name)
        for name in (
            "count_agent_tasks",
            "count_agent_tasks_for_workflow_collection",
            "list_agent_tasks_for_workflow_collection",
        )
    )


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
