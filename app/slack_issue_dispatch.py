from __future__ import annotations

from collections.abc import Collection
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from app.config import Settings
from app.correlation import correlation_id_from_parsed
from app.github_events import GitHubEventType, ParsedGitHubEvent

AGENT_READY_LABEL = "agent-ready"
APPROVED_REPO_FULL_NAMES = {
    "marcus937/Project-Jarvis",
    "marcus937/hermes-runtime-agent",
    "marcus937/jarvis-agent-bus-mcp",
    "marcus937/jarvis-mission-control",
    "marcus937/riseos-agent-orchestrator",
    "marcus937/Rylinn-Field-App-Codex",
}
DEFAULT_BRANCH_RULE = "agent-integration only"
ORCHESTRATOR_SLACK_CHANNEL = "#jarvis-agent-orchestrator"
HERMES_SLACK_CHANNEL = "#jarvis-hermes-runtime"


class SlackIssueDispatchResult(BaseModel):
    attempted: bool = False
    success: bool = False
    issue_key: str | None = None
    correlation_id: str | None = None
    skipped_reason: str | None = None
    error: str | None = None
    message: str | None = None


class SlackIssueDispatchClient(Protocol):
    async def post_message(self, *, channel: str, text: str) -> None:
        ...


class IssueDispatchRegistry(Protocol):
    def already_dispatched(self, issue_key: str) -> bool:
        ...

    def claim_issue_dispatch(self, issue_key: str) -> bool:
        ...

    def mark_dispatched(self, issue_key: str) -> None:
        ...


class SlackClient:
    def __init__(self, *, webhook_url: str | None, bot_token: str | None, http_client: Any | None = None) -> None:
        self._webhook_url = webhook_url
        self._bot_token = bot_token
        self._http_client = http_client or httpx.AsyncClient(timeout=20.0)
        self._owns_http_client = http_client is None

    async def post_message(self, *, channel: str, text: str) -> None:
        if self._webhook_url:
            response = await self._http_client.post(
                self._webhook_url,
                json={"channel": channel, "text": text},
            )
            response.raise_for_status()
            return

        if self._bot_token:
            response = await self._http_client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self._bot_token}"},
                json={"channel": channel, "text": text},
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(str(payload.get("error") or "Slack chat.postMessage failed."))
            return

        raise RuntimeError("Slack webhook URL or bot token is required for Slack dispatch.")

    async def aclose(self) -> None:
        if self._owns_http_client and hasattr(self._http_client, "aclose"):
            await self._http_client.aclose()


class InMemoryDispatchedIssueRegistry:
    def __init__(self) -> None:
        self._issue_keys: set[str] = set()

    def already_dispatched(self, issue_key: str) -> bool:
        return issue_key in self._issue_keys

    def claim_issue_dispatch(self, issue_key: str) -> bool:
        if issue_key in self._issue_keys:
            return False
        self._issue_keys.add(issue_key)
        return True

    def mark_dispatched(self, issue_key: str) -> None:
        self._issue_keys.add(issue_key)

    def reset(self) -> None:
        self._issue_keys.clear()


issue_dispatch_registry = InMemoryDispatchedIssueRegistry()


async def dispatch_ready_issue_to_slack(
    parsed: ParsedGitHubEvent,
    settings: Settings,
    *,
    client: SlackIssueDispatchClient | None = None,
    registry: IssueDispatchRegistry = issue_dispatch_registry,
    approved_repositories: Collection[str] | None = None,
) -> SlackIssueDispatchResult:
    issue_key = _issue_key(parsed)
    correlation_id = correlation_id_from_parsed(parsed)
    skipped_reason = _skip_reason(parsed, approved_repositories=approved_repositories)
    if skipped_reason:
        return SlackIssueDispatchResult(issue_key=issue_key, correlation_id=correlation_id, skipped_reason=skipped_reason)

    if issue_key is None:
        return SlackIssueDispatchResult(correlation_id=correlation_id, skipped_reason="Issue key could not be determined.")

    webhook_url = _orchestrator_slack_webhook_url(settings)
    channel = _orchestrator_slack_channel(settings)
    if not webhook_url and not settings.slack_bot_token:
        return SlackIssueDispatchResult(
            issue_key=issue_key,
            correlation_id=correlation_id,
            skipped_reason="Slack dispatch is not configured.",
        )

    if not registry.claim_issue_dispatch(issue_key):
        return SlackIssueDispatchResult(
            issue_key=issue_key,
            correlation_id=correlation_id,
            skipped_reason="Issue was already dispatched.",
        )

    owns_client = client is None
    client = client or SlackClient(webhook_url=webhook_url, bot_token=settings.slack_bot_token)
    message = build_circuit_slack_message(parsed, channel=channel)
    try:
        await client.post_message(channel=channel, text=message)
    except Exception as exc:
        return SlackIssueDispatchResult(
            attempted=True,
            success=False,
            issue_key=issue_key,
            correlation_id=correlation_id,
            error=str(exc),
            message=message,
        )
    finally:
        if owns_client and hasattr(client, "aclose"):
            await client.aclose()

    registry.mark_dispatched(issue_key)
    return SlackIssueDispatchResult(
        attempted=True,
        success=True,
        issue_key=issue_key,
        correlation_id=correlation_id,
        message=message,
    )


def build_circuit_slack_message(parsed: ParsedGitHubEvent, *, channel: str) -> str:
    labels = ", ".join(_sanitize_slack_text(label) for label in parsed.labels) if parsed.labels else "none"
    title = _sanitize_slack_text(parsed.issue_title or f"Issue #{parsed.issue_number}")
    issue_url = _sanitize_slack_text(parsed.issue_url or "No issue URL provided.")
    repo = _sanitize_slack_text(parsed.repository or "unknown repo")
    correlation_id = _sanitize_slack_text(correlation_id_from_parsed(parsed))
    return (
        "@circuit-forge Circuit task ready\n"
        f"Channel: {_sanitize_slack_text(channel)}\n"
        f"Correlation ID: {correlation_id}\n"
        f"Repo: {repo}\n"
        f"Issue: #{parsed.issue_number} - {title}\n"
        f"Labels: {labels}\n"
        f"URL: {issue_url}\n"
        f"Branch rule: {DEFAULT_BRANCH_RULE}\n"
        "Reminder: no merge, no deploy, and no branch mutation."
    )


def _skip_reason(parsed: ParsedGitHubEvent, *, approved_repositories: Collection[str] | None = None) -> str | None:
    if parsed.event_type != GitHubEventType.ISSUES:
        return "Not a GitHub issues event."
    if parsed.action not in {"opened", "labeled"}:
        return "Issue action is not opened or labeled."
    if not _is_approved_repo(parsed.repository, approved_repositories=approved_repositories):
        return "Repository is not approved for Circuit Slack dispatch."
    if parsed.issue_state and parsed.issue_state != "open":
        return "Issue is not open."
    if AGENT_READY_LABEL not in parsed.labels:
        return "Issue does not have agent-ready label."
    if parsed.action == "labeled" and parsed.action_label != AGENT_READY_LABEL:
        return "Issue was labeled with a non-agent-ready label."
    return None


def _is_approved_repo(repo_full_name: str | None, *, approved_repositories: Collection[str] | None = None) -> bool:
    if repo_full_name is None:
        return False
    return repo_full_name in APPROVED_REPO_FULL_NAMES or (
        approved_repositories is not None and repo_full_name in approved_repositories
    )


def _issue_key(parsed: ParsedGitHubEvent) -> str | None:
    if not parsed.repository or parsed.issue_number is None:
        return None
    return f"{parsed.repository}#{parsed.issue_number}"


def _sanitize_slack_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _orchestrator_slack_webhook_url(settings: Settings) -> str | None:
    if settings.orchestrator_slack_webhook_url:
        return settings.orchestrator_slack_webhook_url
    if settings.hermes_slack_webhook_url and settings.slack_webhook_url == settings.hermes_slack_webhook_url:
        return None
    return settings.slack_webhook_url


def _orchestrator_slack_channel(settings: Settings) -> str:
    return settings.orchestrator_slack_channel or settings.slack_channel or ORCHESTRATOR_SLACK_CHANNEL
