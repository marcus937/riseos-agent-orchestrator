from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.clients.github import GitHubClient
from app.event_store import DebugHealth, EventRecord, event_store
from app.github_context import hydrate_github_context
from app.github_events import UnsupportedGitHubEventError, WebhookAcceptedResponse, parse_github_event
from app.github_writeback import writeback_review_decision
from app.reviewer.openai_review import request_openai_review_decision
from app.review_queue import ReviewProcessResponse, ReviewWorkItem, process_review_work_item, review_queue
from app.review_workflow import build_review_workflow
from app.security import verify_github_signature
from app.storage import SQLiteStateStore, build_sqlite_store


app = FastAPI(title="RiseOS Agent Orchestrator", version="0.1.0")


@app.on_event("startup")
async def startup() -> None:
    app.state.storage = build_sqlite_store(get_settings().orchestrator_db_path)


def _storage() -> SQLiteStateStore | None:
    return getattr(app.state, "storage", None)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/debug/recent-events", response_model=list[EventRecord])
async def recent_events() -> list[EventRecord]:
    storage = _storage()
    if storage is not None:
        return storage.recent_events()
    return event_store.recent_events()


@app.get("/debug/health", response_model=DebugHealth)
async def debug_health() -> DebugHealth:
    storage = _storage()
    if storage is not None:
        return event_store.debug_health(storage.review_queue_counters(), accepted_count=storage.event_count())
    return event_store.debug_health(review_queue.counters())


@app.get("/debug/review-queue", response_model=list[ReviewWorkItem])
async def debug_review_queue() -> list[ReviewWorkItem]:
    storage = _storage()
    if storage is not None:
        return storage.list_review_work_items()
    return review_queue.list_items()


@app.get("/debug/review-queue/{item_id}", response_model=ReviewWorkItem)
async def debug_review_queue_item(item_id: str) -> ReviewWorkItem:
    storage = _storage()
    item = storage.get_review_work_item(item_id) if storage is not None else review_queue.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review work item not found")
    return item


@app.post("/debug/review-queue/{item_id}/process", response_model=ReviewProcessResponse)
async def process_debug_review_queue_item(
    item_id: str,
    settings: Settings = Depends(get_settings),
) -> ReviewProcessResponse:
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
    changed_files: list[str] = []
    diff_summary: str | None = None
    github_context_available = False
    github_context_error: str | None = None

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
        finally:
            await github_client.aclose()

        changed_files = github_context.changed_files
        diff_summary = github_context.diff_summary
        github_context_available = github_context.github_context_available
        github_context_error = github_context.github_context_error

    openai_review = await request_openai_review_decision(
        item,
        settings,
        changed_files=changed_files,
        diff_summary=diff_summary,
        github_context_available=github_context_available,
        github_context_error=github_context_error,
    )

    response = process_review_work_item(
        item,
        decision=openai_review.decision,
        changed_files=changed_files,
        diff_summary=diff_summary,
        github_context_available=github_context_available,
        github_context_error=github_context_error,
        openai_review_attempted=openai_review.attempted,
        openai_review_success=openai_review.success,
        openai_review_error=openai_review.error,
        reviewer_model=openai_review.reviewer_model,
    )

    if not settings.enable_github_writeback:
        return response

    github_client = GitHubClient(token=settings.github_token)
    try:
        writeback = await writeback_review_decision(response, github_client)
    finally:
        await github_client.aclose()

    response.github_writeback_attempted = writeback.attempted
    response.github_writeback_success = writeback.success
    response.github_writeback_error = writeback.error
    return response


@app.post("/webhooks/github", response_model=WebhookAcceptedResponse)
async def github_webhook(
    request: Request,
    x_github_event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
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
    event_record = event_store.record_accepted(parsed)
    work_item = review_queue.create_from_review_event(parsed, workflow)
    storage = _storage()
    if storage is not None:
        storage.save_event_record(event_record)
        if work_item is not None:
            storage.save_review_work_item(work_item)

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
