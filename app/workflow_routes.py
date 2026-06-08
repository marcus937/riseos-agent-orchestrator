import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.event_store import event_store
from app.review_queue import review_queue
from app.storage import SQLiteStateStore
from app.workflows import WorkflowCollection, WorkflowRecord, WorkflowTimeline, build_workflows, find_workflow

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


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


@router.get("", response_model=WorkflowCollection)
async def list_workflows(
    request: Request,
    _: None = Depends(_require_workflow_read_access),
) -> WorkflowCollection:
    return WorkflowCollection(workflows=_build_request_workflows(request))


@router.get("/{workflow_id}", response_model=WorkflowRecord)
async def get_workflow(
    workflow_id: str,
    request: Request,
    _: None = Depends(_require_workflow_read_access),
) -> WorkflowRecord:
    workflow = find_workflow(_build_request_workflows(request), workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return workflow


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
    app.state.workflow_routes_registered = True


def _build_request_workflows(request: Request) -> list[WorkflowRecord]:
    storage = _storage(request)
    if storage is not None:
        return build_workflows(storage.list_review_work_items(), storage.recent_events())
    return build_workflows(review_queue.list_items(), event_store.recent_events())


def _storage(request: Request) -> SQLiteStateStore | None:
    return getattr(request.app.state, "storage", None)
