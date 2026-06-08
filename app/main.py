import hmac
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.clients.github import GitHubClient
from app.event_store import DebugHealth, EventRecord, event_record_from_parsed, event_store, webhook_delivery_key
from app.github_context import hydrate_github_context
from app.github_events import ParsedGitHubEvent, UnsupportedGitHubEventError, WebhookAcceptedResponse, parse_github_event
from app.github_writeback import writeback_review_decision
from app.hermes_dispatch import dispatch_hermes_runtime_validation
from app.operational_logging import (
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
from app.reviewer.decision import ReviewDecisionType
from app.reviewer.openai_review import request_openai_review_decision
from app.review_queue import (
    RecentFailure,
    ReviewLifecycleStage,
    ReviewLifecycleVisibility,
    ReviewProcessResponse,
    ReviewQueueStats,
    ReviewWorkItem,
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
from app.security import verify_github_signature
from app.slack_issue_dispatch import dispatch_ready_issue_to_slack
from app.storage import SQLiteStateStore, build_sqlite_store
from app.task_dispatch import dispatch_next_agent_task


app = FastAPI(title="RiseOS Agent Orchestrator", version="0.1.0")


@app.on_event("startup")
async def startup() -> None:
    settings = get_settings()
    storage = build_sqlite_store(
        settings.orchestrator_db_path,
        max_review_items=settings.orchestrator_max_review_items,
    )
    if storage is not None:
        storage.reclaim_stale_review_claims(older_than_seconds=settings.review_claim_timeout_seconds)
    app.state.storage = storage


def _storage() -> SQLiteStateStore | None:
    return getattr(app.state, "storage", None)


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


@app.get("/debug/recent-events", response_model=list[EventRecord])
async def recent_events(
    _: None = Depends(_require_debug_read_access),
) -> list[EventRecord]:
    return _recent_events()


@app.get("/debug/health", response_model=DebugHealth)
async def debug_health(
    _: None = Depends(_require_debug_read_access),
) -> DebugHealth:
    return _debug_health()


@app.get("/debug/review-queue", response_model=list[ReviewWorkItem])
async def debug_review_queue(
    _: None = Depends(_require_debug_read_access),
) -> list[ReviewWorkItem]:
    return _review_items()


@app.get("/debug/review-queue/stats", response_model=ReviewQueueStats)
async def debug_review_queue_stats(
    _: None = Depends(_require_debug_read_access),
) -> ReviewQueueStats:
    return _review_queue_stats(_review_items())


@app.get("/debug/workers/stats", response_model=WorkerStats)
async def debug_worker_stats(
    _: None = Depends(_require_debug_read_access),
    settings: Settings = Depends(get_settings),
) -> WorkerStats:
    return build_worker_stats(_review_items(), auto_processing_enabled=settings.enable_auto_review_processing)


@app.get("/debug/review-lifecycle", response_model=list[ReviewLifecycleVisibility])
async def debug_review_lifecycle(
    _: None = Depends(_require_debug_read_access),
) -> list[ReviewLifecycleVisibility]:
    return build_lifecycle_visibility(_review_items())


@app.get("/debug/recent-failures", response_model=list[RecentFailure])
async def debug_recent_failures(
    _: None = Depends(_require_debug_read_access),
) -> list[RecentFailure]:
    return build_recent_failures(_review_items())


@app.get("/debug/review-queue/{item_id}", response_model=ReviewWorkItem)
async def debug_review_queue_item(
    item_id: str,
    _: None = Depends(_require_debug_read_access),
) -> ReviewWorkItem:
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
    result = await _process_work_item(item, settings)
    return result


async def _process_work_item(item: ReviewWorkItem, settings: Settings) -> ReviewProcessResponse:
    log_review_processing_started(item)
    record_lifecycle_stage(item, ReviewLifecycleStage.REVIEW_STARTED)
    changed_files: list[str] = []
    diff_summary: str | None = None
    diff_patches: list[dict[str, object]] = []
    patch_truncated = False
    github_context_available = False
    github_context_error: str | None = None
    runtime_evidence_context: list[dict[str, object]] = []
    runtime_evidence_error: str | None = None
    runtime_evidence_truncated = False

    if not settings.enable_github_context_hydration:
        pass
    else:
        github_client = GitHubClient(token=settings.github_token)
        try:
            github_context = await hydrate_github_context(
                item,
                github_client,
                base_branch=settings.base_branch,
            )
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
        runtime_evidence_context = github_context.runtime_evidence_context
        runtime_evidence_error = github_context.runtime_evidence_error
        runtime_evidence_truncated = github_context.runtime_evidence_truncated

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
    log_openai_review_result(
        attempted=openai_review.attempted,
        success=openai_review.success,
        error=openai_review.error,
        reviewer_model=openai_review.reviewer_model,
    )
    if openai_review.attempted:
        record_lifecycle_stage(
            item,
            ReviewLifecycleStage.OPENAI_REVIEW_SUCCEEDED if openai_review.success else ReviewLifecycleStage.OPENAI_REVIEW_FAILED,
            error=openai_review.error,
        )

    response = process_review_work_item(
        item,
        decision=openai_review.decision,
        changed_files=changed_files,
        diff_summary=diff_summary,
        diff_patches=diff_patches,
        patch_truncated=patch_truncated,
        github_context_available=github_context_available,
        github_context_error=github_context_error,
        runtime_evidence_context=runtime_evidence_context,
        runtime_evidence_error=runtime_evidence_error,
        runtime_evidence_truncated=runtime_evidence_truncated,
        openai_review_attempted=openai_review.attempted,
        openai_review_success=openai_review.success,
        openai_review_error=openai_review.error,
        reviewer_model=openai_review.reviewer_model,
    )

    if not settings.enable_github_writeback:
        log_review_completed(response.work_item, decision=response.decision.decision.value)
        record_lifecycle_stage(response.work_item, ReviewLifecycleStage.REVIEW_COMPLETED)
        return response

    log_github_writeback_attempted()
    record_lifecycle_stage(response.work_item, ReviewLifecycleStage.GITHUB_WRITEBACK_STARTED)
    github_client = GitHubClient(token=settings.github_token)
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
        if writeback.success and response.decision.decision == ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW:
            task_dispatch = await dispatch_next_agent_task(
                response.work_item.repo_full_name,
                github_client,
                enabled=settings.enable_task_dispatch,
            )
            response.task_dispatch_attempted = task_dispatch.attempted
            response.task_dispatch_success = task_dispatch.success
            response.task_dispatch_issue_number = task_dispatch.issue_number
            response.task_dispatch_error = task_dispatch.error
    finally:
        await github_client.aclose()

    log_github_writeback_result(
        attempted=response.github_writeback_attempted,
        success=response.github_writeback_success,
        error=response.github_writeback_error,
    )
    log_review_completed(response.work_item, decision=response.decision.decision.value)
    record_lifecycle_stage(response.work_item, ReviewLifecycleStage.REVIEW_COMPLETED)
    return response


def _schedule_auto_process_work_item(
    item: ReviewWorkItem | None,
    settings: Settings,
    storage: SQLiteStateStore | None,
    background_tasks: BackgroundTasks,
) -> bool:
    if item is None or not settings.enable_auto_review_processing:
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
    body = await request.body()
    if not verify_github_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        event_store.record_rejected()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid GitHub webhook signature")

    if not x_github_event:
        event_store.record_rejected()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing GitHub event header")

    try:
        payload: dict[str, Any] = await request.json()
    except Exception as exc:
        event_store.record_rejected()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload") from exc

    try:
        parsed = parse_github_event(x_github_event, payload)
    except UnsupportedGitHubEventError as exc:
        event_store.record_rejected()
        raise HTTPException(status_code=status.HTTP_202_ACCEPTED, detail=str(exc)) from exc

    workflow = build_review_workflow(parsed)
    storage = _storage()
    event_id = webhook_delivery_key(parsed, x_github_delivery)
    if storage is not None:
        event_record = event_record_from_parsed(parsed, event_id=event_id)
        if not storage.save_event_record(event_record):
            log_webhook_duplicate_suppressed(parsed, event_id=event_id)
            event_store.record_duplicate()
            return _webhook_response(parsed, workflow)
    elif event_store.has_event_id(event_id):
        log_webhook_duplicate_suppressed(parsed, event_id=event_id)
        event_store.record_duplicate()
        return _webhook_response(parsed, workflow)
    else:
        event_store.record_accepted(parsed, event_id=event_id)

    log_webhook_accepted(parsed)
    work_item = _create_review_work_item(parsed, workflow.review_context is not None, settings)
    if storage is not None:
        slack_dispatch = await dispatch_ready_issue_to_slack(parsed, settings, registry=storage)
        storage.prune_processed_review_items(settings.orchestrator_max_review_items)
    else:
        slack_dispatch = await dispatch_ready_issue_to_slack(parsed, settings)
    log_slack_issue_dispatch_result(parsed, slack_dispatch)

    github_client = GitHubClient(token=settings.github_token) if settings.enable_github_writeback else None
    try:
        hermes_dispatch = await dispatch_hermes_runtime_validation(parsed, settings, github_client=github_client)
    finally:
        if github_client is not None:
            await github_client.aclose()
    log_hermes_dispatch_result(parsed, hermes_dispatch)

    _schedule_auto_process_work_item(work_item, settings, storage, background_tasks)

    return _webhook_response(parsed, workflow)


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
        commit_sha=workflow.commit_sha,
        review_context=workflow.review_context.model_dump(mode="json") if workflow.review_context else None,
        next_intended_action=workflow.next_intended_action,
    )


def _require_admin_token(settings: Settings, provided_token: str | None) -> None:
    if not settings.orchestrator_admin_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ORCHESTRATOR_ADMIN_TOKEN is required before processing review queue items.",
        )
    if not provided_token or not hmac.compare_digest(provided_token, settings.orchestrator_admin_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid orchestrator admin token")


def _create_review_work_item(
    parsed: ParsedGitHubEvent,
    has_review_context: bool,
    settings: Settings,
) -> ReviewWorkItem | None:
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
