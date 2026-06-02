from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.event_store import DebugHealth, EventRecord, event_store
from app.github_events import UnsupportedGitHubEventError, WebhookAcceptedResponse, parse_github_event
from app.review_workflow import build_review_workflow
from app.security import verify_github_signature


app = FastAPI(title="RiseOS Agent Orchestrator", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/debug/recent-events", response_model=list[EventRecord])
async def recent_events() -> list[EventRecord]:
    return event_store.recent_events()


@app.get("/debug/health", response_model=DebugHealth)
async def debug_health() -> DebugHealth:
    return event_store.debug_health()


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
    event_store.record_accepted(parsed)

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
