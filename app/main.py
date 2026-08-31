import hmac
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.wf20_deployment_resume import (
    claim_waiting_workflow_for_request,
    is_failed_deployment_request,
    is_ready_deployment_request,
    is_waiting_for_deployment_request,
    is_wf20_deployment_status_payload,
    mark_waiting_workflow_failed_for_request,
    mark_waiting_workflow_resumed,
    persist_waiting_for_deployment,
)
from app.wf20_resume_diagnostics import log_starting_hermes
from app.wf20_runtime_validation import (
    AgentBusRuntimeValidationStore,
    BACKEND_ONLY_LABELS,
    DOCUMENTATION_ONLY_LABELS,
    SUPPORTED_PR_ACTIONS,
    _install_agent_bus_runtime_methods,
    _install_github_status_method,
    frontend_validation_profile_for_repo,
    runtime_validation_required_for_parsed,
    runtime_validation_route_reason,
)
from app.wf20_runtime_validation_safe import runtime_validation_request_from_parsed

_install_agent_bus_runtime_methods()
_install_github_status_method()
runtime_validation_store = AgentBusRuntimeValidationStore()

from app.circuit_runtime_validation_routes import (
    register_circuit_runtime_validation_routes,
)
from app.agent_task_review_gate import finalize_review_gated_agent_task
from app.clients.agent_bus import AgentBusClient
from app.clients.github import GitHubClient
from app.event_store import (
    DebugHealth,
    EventRecord,
    event_record_from_parsed,
    event_store,
    webhook_delivery_key,
)
from app.github_context import hydrate_github_context
from app.github_events import (
    ParsedGitHubEvent,
    UnsupportedGitHubEventError,
    WebhookAcceptedResponse,
    parse_github_event,
)
from app.github_writeback import writeback_review_decision
from app.hermes_dispatch import dispatch_hermes_runtime_validation
from app.operational_logging import (
    log_event,
    log_github_writeback_result,
    log_github_writeback_attempted,
    log_hermes_dispatch_result,
    log_openai_review_attempted,
    log_openai_review_result,
    log_queue_item_created,
    log_review_completed,
    log_review_processing_started,
    log_slack_issue_dispatch_result,
    log_webhook_accepted,
    log_webhook_duplicate_suppressed,
)
from app.orchestrator_snapshot import OrchestratorSnapshot, build_orchestrator_snapshot
from app.repository_discovery import (
    RepositoryDiscoveryResult,
    RepositoryRegistryStore,
    build_repository_registry,
    discover_repositories,
    ensure_orchestration_enabled_repository,
    repository_diagnostics,
)
from app.reviewer.decision import ReviewDecisionType
from app.reviewer.openai_review import request_openai_review_decision
from app.review_queue import (
    RecentFailure,
    ReviewLifecycleStage,
    ReviewLifecycleVisibility,
    ReviewProcessResponse,
    ReviewQueueStats,
    ReviewWorkItem,
    ReviewWorkItemStatus,
    WorkerStats,
    build_lifecycle_visibility,
    build_queue_stats,
    build_recent_failures,
    build_worker_stats,
    process_review_work_item,
    record_lifecycle_stage,
    review_queue,
    review_work_item_from_parsed,
)
from app.review_worker import process_queued_review_item
from app.review_workflow import build_review_workflow
from app.runtime_validation_review_bridge import (
    create_runtime_validation_pending_item,
    enqueue_review_from_runtime_validation,
    enqueue_runtime_pending_item,
)
from app.runtime_validation_review_decision import REVIEWER_MODEL as HERMES_BB2_REVIEWER_MODEL
from app.runtime_validation_review_decision import review_decision_from_runtime_validation_context
from app.security import verify_github_signature
from app.slack_issue_dispatch import SlackIssueDispatchResult, dispatch_ready_issue_to_slack
from app.storage import SQLiteStateStore, build_sqlite_store
from app.task_dispatch import dispatch_next_agent_task, dispatch_workflow_chain_continuation, resume_workflow_continuations
from app.workflow_continuation import SQLiteWorkflowContinuationStore, build_workflow_continuation_store
from app.workflow_routes import register_workflow_routes


class AgentTaskSubmission(BaseModel):
    repo_full_name: str
    title: str
    issue_number: int | None = None
    body: str | None = None
    labels: list[str] = Field(default_factory=list)


class AgentTaskSubmissionResponse(BaseModel):
    accepted: bool
    repo_full_name: str
    orchestration_enabled: bool
    auto_registered: bool = False
    issue_number: int | None = None


app = FastAPI(title="RiseOS Agent Orchestrator", version="0.1.0")
app.state.runtime_validation_store = runtime_validation_store
register_workflow_routes(app)
register_circuit_runtime_validation_routes(app)


@app.on_event("startup")
async def startup() -> None:
    settings = get_settings()
    storage = build_sqlite_store(
        settings.orchestrator_db_path,
        max_review_items=settings.orchestrator_max_review_items,
    )
    continuation_store = build_workflow_continuation_store(settings.orchestrator_db_path)
    app.state.workflow_continuation_store = continuation_store
    if storage is not None:
        storage.reclaim_stale_review_claims(older_than_seconds=settings.review_claim_timeout_seconds)
    app.state.storage = storage
    app.state.repository_registry = build_repository_registry(settings)
    app.state.last_repository_discovery_error = None
    if settings.enable_repository_discovery and settings.github_repository_owner:
        github_client = GitHubClient(token=settings.github_token)
        try:
            await discover_repositories(settings.github_repository_owner, settings, github_client, _repository_registry())
        except Exception as exc:
            app.state.last_repository_discovery_error = str(exc)
        finally:
            await github_client.aclose()
    if continuation_store is not None and settings.enable_task_dispatch:
        await _resume_workflow_continuations(settings, continuation_store)


def _storage() -> SQLiteStateStore | None:
    return getattr(app.state, "storage", None)


def _workflow_continuation_store() -> SQLiteWorkflowContinuationStore | None:
    return getattr(app.state, "workflow_continuation_store", None)


async def _resume_workflow_continuations(settings: Settings, continuation_store: SQLiteWorkflowContinuationStore) -> None:
    agent_bus_client = AgentBusClient(base_url=settings.agent_bus_base_url, token=settings.agent_bus_token) if settings.enable_agent_bus_dispatch else None
    try:
        await resume_workflow_continuations(
            continuation_store,
            agent_bus_client=agent_bus_client,
            agent_bus_enabled=settings.enable_agent_bus_dispatch,
        )
    finally:
        if agent_bus_client is not None:
            await agent_bus_client.aclose()


def _repository_registry() -> RepositoryRegistryStore:
    registry = getattr(app.state, "repository_registry", None)
    if registry is None:
        registry = build_repository_registry(get_settings())
        app.state.repository_registry = registry
    return registry


def _approved_repository_names() -> set[str]:
    return {
        record.repo_full_name
        for record in _repository_registry().list_repository_registry_records()
        if record.orchestration_enabled and not record.archived
    }


def _record_repository_event(parsed: ParsedGitHubEvent, *, work_item_created: bool) -> None:
    if not parsed.repository:
        return
    registry = _repository_registry()
    record = registry.get_repository_registry_record(parsed.repository)
    if record is None:
        return
    registry.save_repository_registry_record(
        record.model_copy(
            update={
                "last_event": datetime.now(UTC).isoformat(),
                "last_work_item_generated_at": datetime.now(UTC) if work_item_created else record.last_work_item_generated_at,
            }
        )
    )


def _log_wf20_runtime_gate_decision(
    parsed: ParsedGitHubEvent,
    settings: Settings,
    *,
    has_review_context: bool,
    runtime_gated: bool,
    deployment_status_payload: bool,
) -> None:
    event_type = getattr(parsed.event_type, "value", str(parsed.event_type))
    is_pull_request_event = event_type == "pull_request"
    labels = list(parsed.labels or [])
    normalized_labels = {label.lower() for label in labels}
    documentation_only_or_backend_only = bool(normalized_labels & (DOCUMENTATION_ONLY_LABELS | BACKEND_ONLY_LABELS))
    frontend_validation_profile = frontend_validation_profile_for_repo(parsed.repository, labels=labels)
    validation_route_reason = runtime_validation_route_reason(parsed)
    unsupported_action = is_pull_request_event and parsed.action not in SUPPORTED_PR_ACTIONS
    repo_profile_missing = (
        is_pull_request_event
        and not unsupported_action
        and not documentation_only_or_backend_only
        and not frontend_validation_profile.requires_runtime_validation
        and validation_route_reason is None
    )
    log_event(
        "wf20_runtime_gate_decision",
        event_type=event_type,
        action=parsed.action,
        repository=parsed.repository,
        pull_request_number=parsed.pull_request_number,
        head_sha=parsed.head_sha,
        head_ref=parsed.head_ref,
        base_ref=parsed.base_ref,
        labels=labels,
        has_review_context=has_review_context,
        runtime_gated=runtime_gated,
        deployment_status_payload=deployment_status_payload,
        enable_runtime_validation_review_bridge=settings.enable_runtime_validation_review_bridge,
        validation_route_reason=validation_route_reason,
        frontend_validation_profile={
            "requires_runtime_validation": frontend_validation_profile.requires_runtime_validation,
            "validation_profile": frontend_validation_profile.validation_profile,
        },
        bridge_disabled=not settings.enable_runtime_validation_review_bridge,
        not_pull_request_event=not is_pull_request_event,
        unsupported_action=unsupported_action,
        repo_profile_missing=repo_profile_missing,
        documentation_only_or_backend_only=documentation_only_or_backend_only,
    )


def _require_debug_read_access(
    x_orchestrator_admin_token: Annotated[str | None, Header(alias="X-Orchestrator-Admin-Token")] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.require_admin_token_for_debug_reads:
        return
    _require_admin_token(settings, x_orchestrator_admin_token)


def _review_items() -> list[ReviewWorkItem]:
    storage = _storage()
    if storage is not None:
        return storage.list_review_work_items()
    return review_queue.list_items()


def _recent_events() -> list[EventRecord]:
    storage = _storage()
    if storage is not None:
        return storage.recent_events()
    return event_store.recent_events()


def _review_queue_stats(items: list[ReviewWorkItem]) -> ReviewQueueStats:
    storage = _storage()
    if storage is not None:
        return build_queue_stats(items, counters=storage.review_queue_counters())
    return build_queue_stats(items, counters=review_queue.counters())


def _debug_health() -> DebugHealth:
    storage = _storage()
    if storage is not None:
        return event_store.debug_health(storage.review_queue_counters(), accepted_count=storage.event_count())
    return event_store.debug_health(review_queue.counters())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/agent-tasks", response_model=AgentTaskSubmissionResponse)
async def create_agent_task(
    task: AgentTaskSubmission,
    x_orchestrator_admin_token: Annotated[str | None, Header(alias="X-Orchestrator-Admin-Token")] = None,
    settings: Settings = Depends(get_settings),
) -> AgentTaskSubmissionResponse:
    _require_admin_token(settings, x_orchestrator_admin_token)
    registry = _repository_registry()
    existed_before = registry.get_repository_registry_record(task.repo_full_name) is not None
    record = ensure_orchestration_enabled_repository(
        registry,
        task.repo_full_name,
        trusted_owner=settings.trusted_repository_owner,
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Repository is not orchestration-enabled.")
    if record.archived:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Repository is archived.")
    if not record.orchestration_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Repository is not orchestration-enabled.")
    return AgentTaskSubmissionResponse(
        accepted=True,
        repo_full_name=record.repo_full_name,
        orchestration_enabled=True,
        auto_registered=not existed_before,
        issue_number=task.issue_number,
    )


@app.get("/api/v1/workflow-continuations/{workflow_chain_id}")
async def get_workflow_continuations(workflow_chain_id: str, _: None = Depends(_require_debug_read_access)) -> dict[str, Any]:
    store = _workflow_continuation_store()
    if store is None:
        return {"workflow_chain_id": workflow_chain_id, "continuations": []}
    continuations = store.list_workflow_continuations(workflow_chain_id)
    latest = continuations[-1] if continuations else None
    return {
        "workflow_chain_id": workflow_chain_id,
        "current_workflow_step": latest.current_workflow_step if latest else None,
        "next_workflow_step": latest.next_workflow_step if latest else None,
        "status": latest.status.value if latest else None,
        "work_item_id": latest.current_work_item_id if latest else None,
        "retry_status": latest.status.value if latest and latest.status.value == "RETRY_PENDING" else None,
        "continuations": [continuation.model_dump(mode="json") for continuation in continuations],
    }


@app.get("/api/v1/orchestrator/snapshot", response_model=OrchestratorSnapshot)
async def orchestrator_snapshot(
    _: None = Depends(_require_debug_read_access),
    settings: Settings = Depends(get_settings),
) -> OrchestratorSnapshot:
    items = _review_items()
    return build_orchestrator_snapshot(
        settings=settings,
        health=_debug_health(),
        queue=_review_queue_stats(items),
        worker_stats=build_worker_stats(items, auto_processing_enabled=settings.enable_auto_review_processing),
        lifecycle=build_lifecycle_visibility(items),
        review_items=items,
        events=_recent_events(),
        recent_failures=build_recent_failures(items),
    )


@app.get("/debug/repositories")
async def debug_repositories(_: None = Depends(_require_debug_read_access)) -> dict[str, object]:
    return {"repositories": repository_diagnostics(_repository_registry()), "last_discovery_error": getattr(app.state, "last_repository_discovery_error", None)}


@app.post("/debug/repositories/discover", response_model=RepositoryDiscoveryResult)
async def debug_discover_repositories(
    x_orchestrator_admin_token: Annotated[str | None, Header(alias="X-Orchestrator-Admin-Token")] = None,
    settings: Settings = Depends(get_settings),
) -> RepositoryDiscoveryResult:
    _require_admin_token(settings, x_orchestrator_admin_token)
    if not settings.github_repository_owner:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="GITHUB_REPOSITORY_OWNER is required.")
    github_client = GitHubClient(token=settings.github_token)
    try:
        result = await discover_repositories(settings.github_repository_owner, settings, github_client, _repository_registry())
    finally:
        await github_client.aclose()
    app.state.last_repository_discovery_error = None
    return result


@app.get("/debug/recent-events", response_model=list[EventRecord])
async def recent_events(_: None = Depends(_require_debug_read_access)) -> list[EventRecord]:
    return _recent_events()


@app.get("/debug/health", response_model=DebugHealth)
async def debug_health(_: None = Depends(_require_debug_read_access)) -> DebugHealth:
    return _debug_health()


@app.get("/debug/review-queue", response_model=list[ReviewWorkItem])
async def debug_review_queue(_: None = Depends(_require_debug_read_access)) -> list[ReviewWorkItem]:
    return _review_items()


@app.get("/debug/review-queue/stats", response_model=ReviewQueueStats)
async def debug_review_queue_stats(_: None = Depends(_require_debug_read_access)) -> ReviewQueueStats:
    return _review_queue_stats(_review_items())


@app.get("/debug/workers/stats", response_model=WorkerStats)
async def debug_worker_stats(_: None = Depends(_require_debug_read_access), settings: Settings = Depends(get_settings)) -> WorkerStats:
    return build_worker_stats(_review_items(), auto_processing_enabled=settings.enable_auto_review_processing)


@app.get("/debug/review-lifecycle", response_model=list[ReviewLifecycleVisibility])
async def debug_review_lifecycle(_: None = Depends(_require_debug_read_access)) -> list[ReviewLifecycleVisibility]:
    return build_lifecycle_visibility(_review_items())


@app.get("/debug/recent-failures", response_model=list[RecentFailure])
async def debug_recent_failures(_: None = Depends(_require_debug_read_access)) -> list[RecentFailure]:
    return build_recent_failures(_review_items())


@app.get("/debug/review-queue/{item_id}", response_model=ReviewWorkItem)
async def debug_review_queue_item(item_id: str, _: None = Depends(_require_debug_read_access)) -> ReviewWorkItem:
    storage = _storage()
    item = storage.get_review_work_item(item_id) if storage is not None else review_queue.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review work item not found")
    return item


@app.post("/debug/review-queue/{item_id}/process", response_model=ReviewProcessResponse)
async def process_debug_review_queue_item(
    item_id: str,
    x_orchestrator_admin_token: Annotated[str | None, Header(alias="X-Orchestrator-Admin-Token")] = None,
    settings: Settings = Depends(get_settings),
) -> ReviewProcessResponse:
    _require_admin_token(settings, x_orchestrator_admin_token)
    storage = _storage()
    if storage is not None:
        item = storage.get_review_work_item(item_id)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review work item not found")
        result = await _process_work_item(item, settings)
        storage.save_review_work_item(result.work_item)
        return result
    item = review_queue.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review work item not found")
    return await _process_work_item(item, settings)


async def _process_work_item(item: ReviewWorkItem, settings: Settings) -> ReviewProcessResponse:
    log_review_processing_started(item)
    record_lifecycle_stage(item, ReviewLifecycleStage.REVIEW_STARTED)
    changed_files: list[str] = []
    diff_summary: str | None = None
    diff_patches: list[dict[str, object]] = []
    patch_truncated = False
    github_context_available = False
    github_context_error: str | None = None
    runtime_evidence_context: list[dict[str, object]] = _runtime_evidence_context_from_item(item)
    runtime_evidence_error: str | None = None
    runtime_evidence_truncated = False

    if settings.enable_github_context_hydration:
        github_client = GitHubClient(token=settings.github_token)
        base_branch = item.base_branch or settings.base_branch
        log_event(
            "github_context_hydration_started",
            item_id=item.id,
            repo_full_name=item.repo_full_name,
            pr_number=item.pr_number,
            base_branch=base_branch,
            branch=item.branch,
        )
        try:
            github_context = await hydrate_github_context(item, github_client, base_branch=base_branch)
        except Exception as exc:
            github_context_error = str(exc)
            record_lifecycle_stage(item, ReviewLifecycleStage.REVIEW_FAILED, error=github_context_error)
            raise
        finally:
            await github_client.aclose()

        changed_files = github_context.changed_files
        diff_summary = github_context.diff_summary
        diff_patches = github_context.diff_patches
        patch_truncated = github_context.patch_truncated
        github_context_available = github_context.github_context_available
        github_context_error = github_context.github_context_error
        runtime_evidence_context.extend(github_context.runtime_evidence_context)
        runtime_evidence_error = github_context.runtime_evidence_error
        runtime_evidence_truncated = github_context.runtime_evidence_truncated
        log_event(
            "github_context_hydration_completed",
            item_id=item.id,
            repo_full_name=item.repo_full_name,
            pr_number=item.pr_number,
            base_branch=base_branch,
            branch=item.branch,
            changed_file_count=len(changed_files),
            patch_truncated=patch_truncated,
            error=github_context_error,
        )

    review_decision = review_decision_from_runtime_validation_context(item.runtime_validation_context)
    openai_review_attempted = False
    openai_review_success = False
    openai_review_error: str | None = None
    reviewer_model: str | None = None

    if review_decision is not None:
        reviewer_model = HERMES_BB2_REVIEWER_MODEL
        log_event(
            "bb2_runtime_review_decision_selected",
            item_id=item.id,
            repo_full_name=item.repo_full_name,
            pr_number=item.pr_number,
            validation_id=item.runtime_validation_id,
            validation_status=item.runtime_validation_status,
            decision=review_decision.decision.value,
            reviewer_model=reviewer_model,
        )
    else:
        if settings.enable_openai_review:
            log_openai_review_attempted(reviewer_model=settings.openai_review_model)
            record_lifecycle_stage(item, ReviewLifecycleStage.OPENAI_REVIEW_ATTEMPTED)
        openai_review = await request_openai_review_decision(
            item,
            settings,
            changed_files=changed_files,
            diff_summary=diff_summary,
            diff_patches=diff_patches,
            patch_truncated=patch_truncated,
            github_context_available=github_context_available,
            github_context_error=github_context_error,
            runtime_evidence_context=runtime_evidence_context,
            runtime_evidence_error=runtime_evidence_error,
            runtime_evidence_truncated=runtime_evidence_truncated,
        )
        log_openai_review_result(attempted=openai_review.attempted, success=openai_review.success, error=openai_review.error, reviewer_model=openai_review.reviewer_model)
        if openai_review.attempted:
            record_lifecycle_stage(item, ReviewLifecycleStage.OPENAI_REVIEW_SUCCEEDED if openai_review.success else ReviewLifecycleStage.OPENAI_REVIEW_FAILED, error=openai_review.error)
        review_decision = openai_review.decision
        openai_review_attempted = openai_review.attempted
        openai_review_success = openai_review.success
        openai_review_error = openai_review.error
        reviewer_model = openai_review.reviewer_model

    response = process_review_work_item(
        item,
        decision=review_decision,
        changed_files=changed_files,
        diff_summary=diff_summary,
        diff_patches=diff_patches,
        patch_truncated=patch_truncated,
        github_context_available=github_context_available,
        github_context_error=github_context_error,
        runtime_evidence_context=runtime_evidence_context,
        runtime_evidence_error=runtime_evidence_error,
        runtime_evidence_truncated=runtime_evidence_truncated,
        openai_review_attempted=openai_review_attempted,
        openai_review_success=openai_review_success,
        openai_review_error=openai_review_error,
        reviewer_model=reviewer_model,
    )

    if not settings.enable_github_writeback:
        log_review_completed(
            response.work_item, decision=response.decision.decision.value
        )
        record_lifecycle_stage(
            response.work_item, ReviewLifecycleStage.REVIEW_COMPLETED
        )
        return response

    log_github_writeback_attempted()
    record_lifecycle_stage(
        response.work_item, ReviewLifecycleStage.GITHUB_WRITEBACK_STARTED
    )
    github_client = GitHubClient(token=settings.github_token)
    agent_bus_client = (
        AgentBusClient(
            base_url=settings.agent_bus_base_url, token=settings.agent_bus_token
        )
        if settings.enable_agent_bus_dispatch
        else None
    )
    try:
        writeback = await writeback_review_decision(response, github_client)
        response.github_writeback_attempted = writeback.attempted
        response.github_writeback_success = writeback.success
        response.github_writeback_error = writeback.error
        record_lifecycle_stage(
            response.work_item,
            ReviewLifecycleStage.GITHUB_WRITEBACK_COMPLETED,
            success=writeback.success,
            error=writeback.error,
        )
        if writeback.success:
            review_gate_handled = False
            if agent_bus_client is not None:
                review_gate_handled = await finalize_review_gated_agent_task(
                    response,
                    settings,
                    agent_bus_client=agent_bus_client,
                    dependency_client=github_client,
                )
            task_dispatch = None
            if not review_gate_handled:
                task_dispatch = await dispatch_workflow_chain_continuation(
                    response.work_item,
                    response.decision.decision,
                    enabled=settings.enable_task_dispatch,
                    agent_bus_client=agent_bus_client,
                    agent_bus_enabled=settings.enable_agent_bus_dispatch,
                    continuation_store=_workflow_continuation_store(),
                    base_branch=settings.base_branch or "agent-integration",
                )
            if (
                not review_gate_handled
                and task_dispatch is None
                and response.decision.decision
                == ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW
            ):
                if settings.enable_agent_bus_dispatch:
                    record_lifecycle_stage(
                        response.work_item,
                        ReviewLifecycleStage.AGENT_BUS_DISPATCH_STARTED,
                    )
                task_dispatch = await dispatch_next_agent_task(
                    response.work_item.repo_full_name,
                    github_client,
                    enabled=settings.enable_task_dispatch,
                    agent_bus_client=agent_bus_client,
                    agent_bus_enabled=settings.enable_agent_bus_dispatch,
                    owner_agent=settings.agent_bus_owner_agent,
                    review_agent=settings.agent_bus_review_agent,
                    work_branch=settings.work_branch,
                )
            if task_dispatch is not None:
                _attach_task_dispatch_result(response, task_dispatch)
                if getattr(task_dispatch, "agent_bus_attempted", False):
                    record_lifecycle_stage(
                        response.work_item,
                        ReviewLifecycleStage.AGENT_BUS_DISPATCH_COMPLETED,
                        success=bool(getattr(task_dispatch, "agent_bus_success", False)),
                        error=getattr(task_dispatch, "agent_bus_error", None),
                    )
    finally:
        await github_client.aclose()
        if agent_bus_client is not None:
            await agent_bus_client.aclose()

    log_github_writeback_result(attempted=response.github_writeback_attempted, success=response.github_writeback_success, error=response.github_writeback_error)
    log_review_completed(response.work_item, decision=response.decision.decision.value)
    record_lifecycle_stage(response.work_item, ReviewLifecycleStage.REVIEW_COMPLETED)
    return response


def _attach_task_dispatch_result(response: ReviewProcessResponse, task_dispatch: Any) -> None:
    agent_bus_attempted = bool(getattr(task_dispatch, "agent_bus_attempted", False))
    agent_bus_success = bool(getattr(task_dispatch, "agent_bus_success", False))
    agent_bus_work_item_id = getattr(task_dispatch, "agent_bus_work_item_id", None)
    agent_bus_error = getattr(task_dispatch, "agent_bus_error", None)
    agent_bus_payload = getattr(task_dispatch, "agent_bus_payload", None)
    response.task_dispatch_attempted = bool(getattr(task_dispatch, "attempted", False))
    response.task_dispatch_success = bool(getattr(task_dispatch, "success", False))
    response.task_dispatch_issue_number = getattr(task_dispatch, "issue_number", None)
    response.task_dispatch_error = getattr(task_dispatch, "error", None)
    response.agent_bus_dispatch_attempted = agent_bus_attempted
    response.agent_bus_dispatch_success = agent_bus_success
    response.agent_bus_work_item_id = agent_bus_work_item_id
    response.agent_bus_dispatch_error = agent_bus_error
    response.agent_bus_payload = agent_bus_payload
    if agent_bus_work_item_id is not None:
        response.work_item.agent_bus_work_item_id = agent_bus_work_item_id
    response.work_item.agent_bus_dispatch_error = agent_bus_error


def _runtime_evidence_context_from_item(item: ReviewWorkItem) -> list[dict[str, object]]:
    if item.runtime_validation_context:
        return [item.runtime_validation_context]
    return []


def _schedule_auto_process_work_item(item: ReviewWorkItem | None, settings: Settings, storage: SQLiteStateStore | None, background_tasks: BackgroundTasks) -> bool:
    if item is None or not settings.enable_auto_review_processing:
        return False
    if item.status != ReviewWorkItemStatus.PENDING_REVIEW:
        return False
    background_tasks.add_task(process_queued_review_item, item.id, settings, storage, _process_work_item)
    return True


@app.post("/webhooks/github", response_model=WebhookAcceptedResponse)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
    x_github_delivery: Annotated[str | None, Header(alias="X-GitHub-Delivery")] = None,
    x_hub_signature_256: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
    settings: Settings = Depends(get_settings),
) -> WebhookAcceptedResponse:
    print("### GITHUB_WEBHOOK_ENTERED ###", flush=True)
    body = await request.body()
    if not verify_github_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        log_event(
            "github_webhook_early_exit",
            reason="invalid_signature",
            github_event=x_github_event,
            github_delivery=x_github_delivery,
            body_size=len(body),
        )
        event_store.record_rejected()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid GitHub webhook signature")
    if not x_github_event:
        log_event(
            "github_webhook_early_exit",
            reason="missing_github_event_header",
            github_delivery=x_github_delivery,
            body_size=len(body),
        )
        event_store.record_rejected()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing GitHub event header")
    try:
        payload: dict[str, Any] = await request.json()
    except Exception as exc:
        log_event(
            "github_webhook_early_exit",
            reason="invalid_json_payload",
            github_event=x_github_event,
            github_delivery=x_github_delivery,
            body_size=len(body),
        )
        event_store.record_rejected()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload") from exc
    log_event(
        "github_webhook_before_parse_github_event",
        github_event=x_github_event,
        github_delivery=x_github_delivery,
        body_size=len(body),
        payload_action=payload.get("action"),
    )
    try:
        parsed = parse_github_event(x_github_event, payload)
        print("### AFTER_PARSE ###", flush=True)
    except UnsupportedGitHubEventError as exc:
        log_event(
            "github_webhook_early_exit",
            reason="unsupported_github_event",
            github_event=x_github_event,
            github_delivery=x_github_delivery,
            body_size=len(body),
            payload_action=payload.get("action"),
            error=str(exc),
        )
        event_store.record_rejected()
        raise HTTPException(status_code=status.HTTP_202_ACCEPTED, detail=str(exc)) from exc
    log_event(
        "github_webhook_after_parse_github_event",
        github_event=x_github_event,
        github_delivery=x_github_delivery,
        event_type=getattr(parsed.event_type, "value", str(parsed.event_type)),
        action=parsed.action,
        repository=parsed.repository,
        pull_request_number=parsed.pull_request_number,
        head_sha=parsed.head_sha,
        head_ref=parsed.head_ref,
        base_ref=parsed.base_ref,
    )

    log_event(
        "github_webhook_before_build_review_workflow",
        github_event=x_github_event,
        github_delivery=x_github_delivery,
        event_type=getattr(parsed.event_type, "value", str(parsed.event_type)),
        action=parsed.action,
        repository=parsed.repository,
        pull_request_number=parsed.pull_request_number,
        head_sha=parsed.head_sha,
        head_ref=parsed.head_ref,
        base_ref=parsed.base_ref,
    )
    workflow = build_review_workflow(parsed)
    storage = _storage()
    event_id = webhook_delivery_key(parsed, x_github_delivery)
    if storage is not None:
        event_record = event_record_from_parsed(parsed, event_id=event_id)
        if not storage.save_event_record(event_record):
            log_webhook_duplicate_suppressed(parsed, event_id=event_id)
            event_store.record_duplicate()
            log_event(
                "github_webhook_early_return",
                reason="duplicate_event_storage",
                event_id=event_id,
                github_event=x_github_event,
                github_delivery=x_github_delivery,
                event_type=getattr(parsed.event_type, "value", str(parsed.event_type)),
                action=parsed.action,
                repository=parsed.repository,
                pull_request_number=parsed.pull_request_number,
                head_sha=parsed.head_sha,
            )
            return _webhook_response(parsed, workflow)
    elif event_store.has_event_id(event_id):
        log_webhook_duplicate_suppressed(parsed, event_id=event_id)
        event_store.record_duplicate()
        log_event(
            "github_webhook_early_return",
            reason="duplicate_event_memory",
            event_id=event_id,
            github_event=x_github_event,
            github_delivery=x_github_delivery,
            event_type=getattr(parsed.event_type, "value", str(parsed.event_type)),
            action=parsed.action,
            repository=parsed.repository,
            pull_request_number=parsed.pull_request_number,
            head_sha=parsed.head_sha,
        )
        return _webhook_response(parsed, workflow)
    else:
        event_store.record_accepted(parsed, event_id=event_id)

    log_webhook_accepted(parsed)
    has_review_context = workflow.review_context is not None
    log_event(
        "github_webhook_before_runtime_validation_required",
        github_event=x_github_event,
        github_delivery=x_github_delivery,
        event_id=event_id,
        event_type=getattr(parsed.event_type, "value", str(parsed.event_type)),
        action=parsed.action,
        repository=parsed.repository,
        pull_request_number=parsed.pull_request_number,
        head_sha=parsed.head_sha,
        head_ref=parsed.head_ref,
        base_ref=parsed.base_ref,
        has_review_context=has_review_context,
        enable_runtime_validation_review_bridge=settings.enable_runtime_validation_review_bridge,
    )
    runtime_gated = runtime_validation_required_for_parsed(parsed, settings, has_review_context=has_review_context)
    print(f"### RUNTIME_GATED={runtime_gated} ###", flush=True)
    deployment_status_payload = is_wf20_deployment_status_payload(parsed)
    _log_wf20_runtime_gate_decision(
        parsed,
        settings,
        has_review_context=has_review_context,
        runtime_gated=runtime_gated,
        deployment_status_payload=deployment_status_payload,
    )
    if runtime_gated and not deployment_status_payload:
        work_item = enqueue_runtime_pending_item(create_runtime_validation_pending_item(parsed), storage=storage, max_review_items=settings.orchestrator_max_review_items)
        log_queue_item_created(work_item)
    elif runtime_gated:
        work_item = None
    else:
        work_item = _create_review_work_item(parsed, has_review_context, settings)
    _record_repository_event(parsed, work_item_created=work_item is not None)

    approved_repositories = _approved_repository_names()
    if storage is not None:
        slack_dispatch = await _dispatch_ready_issue_to_slack(
            parsed,
            settings,
            registry=storage,
            approved_repositories=approved_repositories,
        )
        storage.prune_processed_review_items(settings.orchestrator_max_review_items)
    else:
        slack_dispatch = await _dispatch_ready_issue_to_slack(
            parsed,
            settings,
            approved_repositories=approved_repositories,
        )
    log_slack_issue_dispatch_result(parsed, slack_dispatch)

    if runtime_gated:
        github_client = GitHubClient(token=settings.github_token) if settings.github_token else None
        try:
            validation_request = await runtime_validation_request_from_parsed(parsed, settings, github_client=github_client)
            if is_waiting_for_deployment_request(validation_request):
                if work_item is not None:
                    work_item = persist_waiting_for_deployment(validation_request, work_item, storage=storage)
            elif deployment_status_payload and is_failed_deployment_request(validation_request):
                work_item = mark_waiting_workflow_failed_for_request(validation_request, parsed, storage=storage)
            elif deployment_status_payload and is_ready_deployment_request(validation_request):
                resumed_item = claim_waiting_workflow_for_request(validation_request, parsed, storage=storage)
                if resumed_item is not None:
                    waiting_context = resumed_item.runtime_validation_context or {}
                    if waiting_context.get("workflow_id"):
                        validation_request.workflow_id = str(waiting_context["workflow_id"])
                    if waiting_context.get("correlation_id"):
                        validation_request.correlation_id = str(waiting_context["correlation_id"])
                    log_starting_hermes(validation_request, runtime_validation_id=resumed_item.runtime_validation_id)
                    validation = await runtime_validation_store.trigger(validation_request, settings)
                    resumed_item = mark_waiting_workflow_resumed(resumed_item, storage=storage)
                    work_item = enqueue_review_from_runtime_validation(validation, settings, storage=storage, existing_item=resumed_item)
            elif not deployment_status_payload:
                validation = await runtime_validation_store.trigger(validation_request, settings)
                work_item = enqueue_review_from_runtime_validation(validation, settings, storage=storage, existing_item=work_item)
        finally:
            if github_client is not None:
                await github_client.aclose()
    else:
        github_client = GitHubClient(token=settings.github_token) if settings.enable_github_writeback else None
        try:
            hermes_dispatch = await dispatch_hermes_runtime_validation(parsed, settings, github_client=github_client)
        finally:
            if github_client is not None:
                await github_client.aclose()
        log_hermes_dispatch_result(parsed, hermes_dispatch)

    _schedule_auto_process_work_item(work_item, settings, storage, background_tasks)
    return _webhook_response(parsed, workflow)


async def _dispatch_ready_issue_to_slack(
    parsed: ParsedGitHubEvent,
    settings: Settings,
    *,
    registry: Any | None = None,
    approved_repositories: set[str] | None = None,
) -> SlackIssueDispatchResult:
    try:
        return await dispatch_ready_issue_to_slack(
            parsed,
            settings,
            registry=registry,
            approved_repositories=approved_repositories,
        )
    except TypeError as exc:
        if "approved_repositories" not in str(exc):
            raise
        if registry is not None:
            return await dispatch_ready_issue_to_slack(parsed, settings, registry=registry)
        return await dispatch_ready_issue_to_slack(parsed, settings)


def _webhook_response(parsed: ParsedGitHubEvent, workflow: Any) -> WebhookAcceptedResponse:
    return WebhookAcceptedResponse(
        event_type=parsed.event_type,
        repository=parsed.repository,
        repo=workflow.repo,
        action=parsed.action,
        event_accepted=workflow.event_accepted,
        task_state=workflow.task_state.value,
        issue_number=workflow.issue_number,
        pull_request_number=workflow.pull_request_number,
        commit_sha=parsed.head_sha,
        review_context=workflow.review_context.model_dump(mode="json") if workflow.review_context else None,
        next_intended_action=workflow.next_intended_action,
    )


def _require_admin_token(settings: Settings, provided_token: str | None) -> None:
    if not settings.orchestrator_admin_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="ORCHESTRATOR_ADMIN_TOKEN is required before processing review queue items.")
    if not provided_token or not hmac.compare_digest(settings.orchestrator_admin_token, provided_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid orchestrator admin token")


def _create_review_work_item(parsed: ParsedGitHubEvent, has_review_context: bool, settings: Settings) -> ReviewWorkItem | None:
    if not has_review_context:
        return None
    item = review_work_item_from_parsed(parsed)
    storage = _storage()
    if storage is not None:
        duplicate = storage.find_pending_duplicate(item)
        if duplicate is not None:
            return duplicate
        storage.save_review_work_item(item)
        log_queue_item_created(item)
        return item
    duplicate = review_queue.find_pending_duplicate(item)
    if duplicate is not None:
        return duplicate
    item = review_queue.add_if_absent(item)
    review_queue.prune_processed(settings.orchestrator_max_review_items)
    log_queue_item_created(item)
    return item