from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from app.clients.slack import MissingSlackConfigError, SlackClient
from app.config import Settings, get_settings
from app.github_events import UnsupportedGitHubEventError, WebhookAcceptedResponse, parse_github_event
from app.review_workflow import build_review_workflow
from app.security import verify_github_signature


app = FastAPI(title="RiseOS Agent Orchestrator", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/github", response_model=WebhookAcceptedResponse)
async def github_webhook(
    request: Request,
    x_github_event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
    x_hub_signature_256: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
    settings: Settings = Depends(get_settings),
) -> WebhookAcceptedResponse:
    body = await request.body()
    if not verify_github_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid GitHub webhook signature")

    if not x_github_event:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing GitHub event header")

    try:
        payload: dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload") from exc

    try:
        parsed = parse_github_event(x_github_event, payload)
    except UnsupportedGitHubEventError as exc:
        raise HTTPException(status_code=status.HTTP_202_ACCEPTED, detail=str(exc)) from exc

    workflow = build_review_workflow(parsed)
    slack_task_posted = False
    if workflow.requeue_context:
        slack_task_posted = await _post_slack_requeue_task(settings, workflow.requeue_context)

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
        requeue_context=workflow.requeue_context.model_dump(mode="json") if workflow.requeue_context else None,
        slack_task_posted=slack_task_posted,
        next_intended_action=workflow.next_intended_action,
    )


async def _post_slack_requeue_task(settings: Settings, requeue_context: Any) -> bool:
    client = SlackClient(token=settings.slack_bot_token, channel=settings.slack_task_channel)
    try:
        await client.post_requeue_task(requeue_context)
    except MissingSlackConfigError:
        return False
    finally:
        await client.aclose()
    return True
