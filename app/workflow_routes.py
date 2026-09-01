import hmac
from datetime import UTC, datetime, timedelta
from time import perf_counter
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
from app.operational_logging import log_polling_endpoint_response, serialized_json_bytes
from app.review_queue import ReviewWorkItem, review_queue
from app.storage import SQLiteStateStore
from app.workflows import (
    WORKFLOW_LIST_DEFAULT_LIMIT,
    WORKFLOW_LIST_DEFAULT_RECENT_DAYS,
    WORKFLOW_LIST_MAX_LIMIT,
    WORKFLOW_LIST_MAX_OFFSET,
    WORKFLOW_LIST_MAX_RECENT_DAYS,
    WorkflowCollection,
    WorkflowListFilter,
    WorkflowPaginationMetadata,
    WorkflowRecord,
    WorkflowTimeline,
    build_workflow_collection,
    build_workflow_summaries,
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
    offset: Annotated[int, Query(ge=0, le=WORKFLOW_LIST_MAX_OFFSET)] = 0,
    workflow_filter: Annotated[WorkflowListFilter, Query(alias="filter")] = WorkflowListFilter.ACTIVE_RECENT,
    recent_days: Annotated[int, Query(ge=1, le=WORKFLOW_LIST_MAX_RECENT_DAYS)] = WORKFLOW_LIST_DEFAULT_RECENT_DAYS,
    _: None = Depends(_require_workflow_read_access),
    settings: Settings = Depends(get_settings),
) -> WorkflowCollection:
    started_at = perf_counter()
    collection = _build_request_workflow_collection(
        request,
        settings,
        limit=limit,
        offset=offset,
        workflow_filter=workflow_filter,
        recent_days=recent_days,
    )
    pagination = collection.pagination
    log_polling_endpoint_response(
        endpoint="/api/v1/workflows",
        duration_seconds=perf_counter() - started_at,
        returned_count=pagination.returned if pagination is not None else len(collection.workflows),
        total_count=pagination.total if pagination is not None else len(collection.workflows),
        serialized_bytes=serialized_json_bytes(collection),
        limit=pagination.limit if pagination is not None else limit,
        offset=pagination.offset if pagination is not None else offset,
        workflow_filter=(
            pagination.filter.value
            if pagination is not None
            else WorkflowListFilter(workflow_filter).value
        ),
        recent_days=pagination.recent_days if pagination is not None else recent_days,
    )
    return collection


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
    bounded_offset = min(max(offset, 0), WORKFLOW_LIST_MAX_OFFSET)
    bounded_recent_days = min(max(recent_days, 1), WORKFLOW_LIST_MAX_RECENT_DAYS)
    normalized_filter = WorkflowListFilter(workflow_filter)
    workflow_filter_value = normalized_filter.value
    now = datetime.now(UTC)
    recent_since = now - timedelta(days=bounded_recent_days)
    # A workflow past this per-source window cannot appear before the end of the
    # merged global page, so detail hydration is unnecessary for list polling.
    candidate_limit = bounded_offset + bounded_limit
    total = _count_bounded_storage_normalized_workflows(
        storage,
        agent_store,
        workflow_filter=workflow_filter_value,
        recent_since=recent_since,
    )
    unfiltered_total = (
        total
        if normalized_filter == WorkflowListFilter.ALL
        else _count_bounded_storage_normalized_workflows(
            storage,
            agent_store,
            workflow_filter=WorkflowListFilter.ALL.value,
        )
    )
    if bounded_offset >= total:
        return WorkflowCollection(
            workflows=[],
            pagination=WorkflowPaginationMetadata(
                limit=bounded_limit,
                offset=bounded_offset,
                returned=0,
                total=total,
                unfiltered_total=unfiltered_total,
                truncated=False,
                has_next=False,
                next_offset=None,
                filter=normalized_filter,
                recent_days=bounded_recent_days,
            ),
        )
    review_items = storage.list_review_work_item_summary_records_for_workflow_collection(
        limit=candidate_limit,
        workflow_filter=workflow_filter_value,
        recent_since=recent_since,
    )
    agent_task_summaries = (
        agent_store.list_agent_task_workflow_summaries_for_collection(
            limit=candidate_limit,
            workflow_filter=workflow_filter_value,
            recent_since=recent_since,
        )
        if agent_store is not None
        else []
    )
    event_candidates = storage.list_event_workflow_summary_records_for_collection(
        limit=candidate_limit,
        workflow_filter=workflow_filter_value,
        recent_since=recent_since,
    )
    workflows = build_workflow_summaries(review_items, event_candidates, agent_task_summaries)
    page = workflows[bounded_offset : bounded_offset + bounded_limit]
    next_offset_candidate = bounded_offset + bounded_limit
    truncated = next_offset_candidate < total
    next_offset = next_offset_candidate if truncated and next_offset_candidate <= WORKFLOW_LIST_MAX_OFFSET else None
    return WorkflowCollection(
        workflows=page,
        pagination=WorkflowPaginationMetadata(
            limit=bounded_limit,
            offset=bounded_offset,
            returned=len(page),
            total=total,
            unfiltered_total=unfiltered_total,
            truncated=truncated,
            has_next=next_offset is not None,
            next_offset=next_offset,
            filter=normalized_filter,
            recent_days=bounded_recent_days,
        ),
    )


def _count_bounded_storage_normalized_workflows(
    storage: SQLiteStateStore,
    agent_store: AgentTaskStore | None,
    *,
    workflow_filter: str,
    recent_since: datetime | None = None,
) -> int:
    # Event workflow counts apply the same review-item identity suppression as
    # summary listing, so the sum is normalized workflow records, not raw rows.
    return (
        storage.count_review_work_items_for_workflow_collection(
            workflow_filter=workflow_filter,
            recent_since=recent_since,
        )
        + (
            agent_store.count_agent_tasks_for_workflow_collection(
                workflow_filter=workflow_filter,
                recent_since=recent_since,
            )
            if agent_store is not None
            else 0
        )
        + storage.count_event_records_for_workflow_collection(
            workflow_filter=workflow_filter,
            recent_since=recent_since,
        )
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

    legacy_workflow = _build_request_workflow(request, settings, workflow_id)
    if legacy_workflow is not None:
        return legacy_workflow

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")


@router.get("/{workflow_id}/timeline", response_model=WorkflowTimeline)
async def get_workflow_timeline(
    workflow_id: str,
    request: Request,
    _: None = Depends(_require_workflow_read_access),
    settings: Settings = Depends(get_settings),
) -> WorkflowTimeline:
    workflow = _build_request_workflow(request, settings, workflow_id)
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


def _build_request_workflow(
    request: Request,
    settings: Settings,
    workflow_id: str,
) -> WorkflowRecord | None:
    storage = _storage(request)
    if storage is not None:
        workflow = _build_storage_workflow(request, settings, storage, workflow_id)
        if workflow is not None:
            return workflow
    return find_workflow(_build_request_workflows(request), workflow_id)


def _build_storage_workflow(
    request: Request,
    settings: Settings,
    storage: SQLiteStateStore,
    workflow_id: str,
) -> WorkflowRecord | None:
    agent_task = _agent_task_for_workflow_id(request, settings, workflow_id)
    if agent_task is not None:
        return build_workflows([], [], [agent_task])[0]

    review_item = _review_work_item_for_workflow_id(storage, workflow_id)
    if review_item is not None:
        return build_workflows([review_item], [], [])[0]

    if hasattr(storage, "list_event_records_for_workflow_id"):
        event_records = storage.list_event_records_for_workflow_id(workflow_id)
        if event_records:
            workflows = build_workflows([], event_records, [])
            return workflows[0] if workflows else None

    if hasattr(storage, "get_event_record_for_workflow_id"):
        event_record = storage.get_event_record_for_workflow_id(workflow_id)
        if event_record is not None:
            return build_workflows([], [event_record], [])[0]

    return None


def _agent_task_for_workflow_id(
    request: Request,
    settings: Settings,
    workflow_id: str,
) -> AgentTask | None:
    prefix = "wf-agent-task-"
    if not workflow_id.startswith(prefix):
        return None
    agent_store = _agent_task_store_for_read(request, settings)
    if agent_store is None:
        return None
    return agent_store.get_agent_task(workflow_id.removeprefix(prefix))


def _review_work_item_for_workflow_id(
    storage: SQLiteStateStore,
    workflow_id: str,
) -> ReviewWorkItem | None:
    prefix = "wf-"
    if not workflow_id.startswith(prefix):
        return None
    return storage.get_review_work_item(workflow_id.removeprefix(prefix))


def _agent_task_store_for_read(request: Request, settings: Settings) -> AgentTaskStore | None:
    storage = _storage(request)
    storage_db_path = getattr(storage, "db_path", None) if storage is not None else None
    agent_task_db_path = settings.orchestrator_db_path or (
        str(storage_db_path) if storage_db_path is not None else None
    )
    store = getattr(request.app.state, "agent_task_store", None)
    if store is not None:
        store_db_path = getattr(store, "db_path", None)
        if store_db_path is None:
            if storage is None:
                return store
        elif str(store_db_path) == agent_task_db_path:
            return store
        elif storage is None:
            return None
    if storage is not None and not agent_task_db_path:
        return None
    store = build_agent_task_store(agent_task_db_path)
    if storage is not None and getattr(store, "db_path", None) is None:
        return None
    return store


def _supports_bounded_workflow_storage(storage: object) -> bool:
    return all(
        hasattr(storage, name)
        for name in (
            "count_event_records_for_workflow_collection",
            "count_review_work_items_for_workflow_collection",
            "list_event_workflow_summary_records_for_collection",
            "list_review_work_item_summary_records_for_workflow_collection",
        )
    )


def _supports_bounded_agent_task_store(agent_store: object | None) -> bool:
    if agent_store is None:
        return True
    return all(
        hasattr(agent_store, name)
        for name in (
            "count_agent_tasks_for_workflow_collection",
            "list_agent_task_workflow_summaries_for_collection",
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
