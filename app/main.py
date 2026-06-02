from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.github_events import UnsupportedGitHubEventError, WebhookAcceptedResponse, parse_github_event
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

    return WebhookAcceptedResponse(
        event_type=parsed.event_type,
        repository=parsed.repository,
        action=parsed.action,
    )
