from __future__ import annotations

import json
import logging
from typing import Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from app.config import Settings


logger = logging.getLogger("riseos_agent_orchestrator")

CIRCUIT_AGENT_ALIASES = {"circuit", "circuit-forge", "circuit forge"}


class CircuitAgentTriggerResult(BaseModel):
    attempted: bool = False
    success: bool = False
    status_code: int | None = None
    skipped_reason: str | None = None
    error: str | None = None
    message: str | None = None
    failure_classification: str | None = None


class CircuitAgentTriggerResponse(BaseModel):
    status_code: int
    text: str = ""


class CircuitAgentTriggerClient(Protocol):
    async def post_wakeup(self, *, url: str, token: str, message: str) -> CircuitAgentTriggerResponse:
        ...


class CircuitAgentTriggerHTTPClient:
    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http_client = http_client
        self._owns_http_client = http_client is None

    async def post_wakeup(self, *, url: str, token: str, message: str) -> CircuitAgentTriggerResponse:
        normalized_token = _normalize_access_token(token)
        if not normalized_token:
            raise ValueError("Circuit agent access token is empty.")

        response = await self._client.post(
            url,
            headers={
                "Authorization": f"Bearer {normalized_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"input": message},
        )
        return CircuitAgentTriggerResponse(status_code=response.status_code, text=response.text)

    async def aclose(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=20.0)
        return self._http_client


async def wake_circuit_agent_for_work(
    settings: Settings,
    *,
    target_agent: str | None = None,
    owner_agent: str | None = None,
    repo_full_name: str | None = None,
    issue_number: int | None = None,
    workflow_id: str | None = None,
    work_item_id: str | None = None,
    client: CircuitAgentTriggerClient | None = None,
) -> CircuitAgentTriggerResult:
    if not is_circuit_agent(target_agent) and not is_circuit_agent(owner_agent):
        return CircuitAgentTriggerResult(skipped_reason="Work is not owned by Circuit.")

    trigger_url = _normalize_optional_text(settings.circuit_agent_trigger_url)
    access_token = _normalize_access_token(settings.circuit_agent_access_token)
    message = build_circuit_wakeup_message(
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        workflow_id=workflow_id,
        work_item_id=work_item_id,
    )
    if not trigger_url or not access_token:
        return CircuitAgentTriggerResult(
            skipped_reason="Circuit agent trigger is not configured.",
            message=message,
        )
    if not _is_trigger_url(trigger_url):
        return CircuitAgentTriggerResult(
            skipped_reason="Circuit agent trigger URL must include /trigger.",
            message=message,
        )

    owns_client = client is None
    client = client or CircuitAgentTriggerHTTPClient()
    try:
        response = await client.post_wakeup(url=trigger_url, token=access_token, message=message)
        status_code = response.status_code
        _log_circuit_wakeup_attempted(
            trigger_url=trigger_url,
            settings=settings,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            workflow_id=workflow_id,
            status_code=status_code,
        )
        if status_code < 200 or status_code >= 300:
            failure_classification = _classify_wakeup_failure(status_code)
            response_body = _truncate(_redact_sensitive_text(response.text, settings), limit=1000)
            _log_circuit_wakeup_warning(
                error=f"Circuit agent wakeup failed with status {status_code}.",
                repo_full_name=repo_full_name,
                issue_number=issue_number,
                workflow_id=workflow_id,
                status_code=status_code,
                response_body=response_body,
                failure_classification=failure_classification,
            )
            return CircuitAgentTriggerResult(
                attempted=True,
                success=False,
                status_code=status_code,
                error=f"Circuit agent wakeup failed with status {status_code}.",
                message=message,
                failure_classification=failure_classification,
            )
    except Exception as exc:
        error = _redact_sensitive_text(str(exc), settings)
        _log_circuit_wakeup_attempted(
            trigger_url=trigger_url,
            settings=settings,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            workflow_id=workflow_id,
            status_code=None,
        )
        _log_circuit_wakeup_warning(
            error=error,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            workflow_id=workflow_id,
            status_code=None,
            response_body=None,
            failure_classification=None,
        )
        return CircuitAgentTriggerResult(
            attempted=True,
            success=False,
            error=error,
            message=message,
        )
    finally:
        if owns_client and hasattr(client, "aclose"):
            await client.aclose()  # type: ignore[attr-defined]

    return CircuitAgentTriggerResult(
        attempted=True,
        success=True,
        status_code=status_code,
        message=message,
    )


def is_circuit_agent(agent_name: str | None) -> bool:
    if not agent_name:
        return False
    return _normalize_agent_name(agent_name) in CIRCUIT_AGENT_ALIASES


def build_circuit_wakeup_message(
    *,
    repo_full_name: str | None = None,
    issue_number: int | None = None,
    workflow_id: str | None = None,
    work_item_id: str | None = None,
) -> str:
    parts = [
        "Circuit Forge wake up and check your Agent Bus inbox. Only work on an explicit Agent Bus assigned work item. "
        "If no assigned work item exists, report idle and stop. Do not search GitHub issues independently unless the "
        "assigned work item explicitly instructs you to do so.",
    ]
    if repo_full_name:
        parts.append(f"Repository: {repo_full_name}.")
    if issue_number is not None:
        parts.append(f"Assigned GitHub issue: #{issue_number}.")
    if workflow_id:
        parts.append(f"Workflow ID: {workflow_id}.")
    if work_item_id:
        parts.append(f"Work item ID: {work_item_id}.")
    return " ".join(parts)


def _normalize_agent_name(agent_name: str) -> str:
    return agent_name.strip().lower().replace("_", "-")


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_access_token(value: str | None) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    if normalized.lower().startswith("bearer "):
        normalized = normalized[7:].strip()
    return normalized or None


def _classify_wakeup_failure(status_code: int | None) -> str | None:
    if status_code == 401:
        return "token_mismatch_or_stale_token"
    if status_code == 403:
        return "agent_access_forbidden"
    return None


def _log_circuit_wakeup_warning(
    *,
    error: str,
    repo_full_name: str | None,
    issue_number: int | None,
    workflow_id: str | None,
    status_code: int | None,
    response_body: str | None,
    failure_classification: str | None,
) -> None:
    logger.warning(
        json.dumps(
            {
                "event": "circuit_agent_wakeup_failed",
                "repo_full_name": repo_full_name,
                "issue_number": issue_number,
                "workflow_id": workflow_id,
                "status_code": status_code,
                "response_body": response_body,
                "error": error,
                "failure_classification": failure_classification,
            },
            sort_keys=True,
        )
    )


def _log_circuit_wakeup_attempted(
    *,
    trigger_url: str,
    settings: Settings,
    repo_full_name: str | None,
    issue_number: int | None,
    workflow_id: str | None,
    status_code: int | None,
) -> None:
    logger.info(
        json.dumps(
            {
                "event": "circuit_agent_wakeup_attempted",
                "target": _safe_url_target(trigger_url, settings),
                "repo_full_name": repo_full_name,
                "issue_number": issue_number,
                "workflow_id": workflow_id,
                "status_code": status_code,
            },
            sort_keys=True,
        )
    )


def _redact_sensitive_text(value: str, settings: Settings) -> str:
    redacted = value
    for secret in _sensitive_values(settings):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _sensitive_values(settings: Settings) -> tuple[str, ...]:
    values: list[str] = []
    raw_token = settings.circuit_agent_access_token
    normalized_token = _normalize_access_token(raw_token)
    for secret in (raw_token, normalized_token):
        if secret and secret not in values:
            values.append(secret)
    return tuple(values)


def _safe_url_target(url: str, settings: Settings) -> str:
    parsed = urlparse(url)
    target = f"{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path
    return _redact_sensitive_text(target or "unknown", settings)


def _is_trigger_url(url: str) -> bool:
    return urlparse(url).path.rstrip("/").endswith("/trigger")


def _truncate(value: str, *, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]
