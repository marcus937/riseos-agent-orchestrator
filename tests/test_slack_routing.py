import asyncio
from typing import Any

from app.config import Settings, get_settings
from app.github_events import parse_github_event
from app.slack_issue_dispatch import (
    AGENT_READY_LABEL,
    InMemoryDispatchedIssueRegistry,
    _route_channel,
    _route_webhook_url,
    _slack_route_for_text,
    dispatch_ready_issue_to_slack,
)


class FakeSlackClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def post_message(self, *, channel: str, text: str) -> None:
        self.messages.append((channel, text))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def issue_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
        "sender": {"login": "marcus"},
        "issue": {
            "number": 66,
            "title": "Split Slack Routing Between Orchestrator and Hermes",
            "state": "open",
            "html_url": "https://github.com/marcus937/riseos-agent-orchestrator/issues/66",
            "labels": [{"name": AGENT_READY_LABEL}],
        },
    }


def test_orchestrator_notification_uses_orchestrator_channel() -> None:
    parsed = parse_github_event("issues", issue_payload())
    client = FakeSlackClient()

    result = run(
        dispatch_ready_issue_to_slack(
            parsed,
            Settings(
                orchestrator_slack_webhook_url="https://hooks.slack.test/orchestrator",
                orchestrator_slack_channel="#jarvis-agent-orchestrator",
                hermes_slack_webhook_url="https://hooks.slack.test/hermes",
                hermes_slack_channel="#jarvis-hermes-runtime",
            ),
            client=client,
            registry=InMemoryDispatchedIssueRegistry(),
        )
    )

    assert result.success is True
    assert client.messages == [("#jarvis-agent-orchestrator", result.message)]
    assert "Channel: #jarvis-agent-orchestrator" in result.message


def test_hermes_runtime_message_routes_to_hermes_destination(monkeypatch: Any) -> None:
    monkeypatch.setenv("ORCHESTRATOR_SLACK_WEBHOOK_URL", "https://hooks.slack.test/orchestrator")
    monkeypatch.setenv("ORCHESTRATOR_SLACK_CHANNEL", "#jarvis-agent-orchestrator")
    monkeypatch.setenv("HERMES_SLACK_WEBHOOK_URL", "https://hooks.slack.test/hermes")
    monkeypatch.setenv("HERMES_SLACK_CHANNEL", "#jarvis-hermes-runtime")

    route = _slack_route_for_text("Hermes validation complete\nStatus: PASSED")

    assert route == "hermes"
    assert _route_webhook_url(route, fallback="https://hooks.slack.test/fallback") == "https://hooks.slack.test/hermes"
    assert _route_channel(route, fallback="#jarvis-agent-orchestrator") == "#jarvis-hermes-runtime"


def test_legacy_slack_configuration_remains_compatible(monkeypatch: Any) -> None:
    monkeypatch.delenv("ORCHESTRATOR_SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_SLACK_CHANNEL", raising=False)
    monkeypatch.delenv("HERMES_SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("HERMES_SLACK_CHANNEL", raising=False)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/legacy")
    monkeypatch.setenv("SLACK_CHANNEL", "#project_riseos")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.orchestrator_slack_webhook_url == "https://hooks.slack.test/legacy"
    assert settings.orchestrator_slack_channel == "#project_riseos"
    assert settings.hermes_slack_webhook_url == "https://hooks.slack.test/legacy"
    assert settings.hermes_slack_channel == "#project_riseos"
    assert _route_webhook_url("hermes", fallback=None) == "https://hooks.slack.test/legacy"
    assert _route_channel("hermes", fallback="#jarvis-agent-orchestrator") == "#project_riseos"


def test_orchestrator_dispatch_does_not_duplicate_messages() -> None:
    parsed = parse_github_event("issues", issue_payload())
    client = FakeSlackClient()
    registry = InMemoryDispatchedIssueRegistry()
    settings = Settings(orchestrator_slack_webhook_url="https://hooks.slack.test/orchestrator")

    first = run(dispatch_ready_issue_to_slack(parsed, settings, client=client, registry=registry))
    second = run(dispatch_ready_issue_to_slack(parsed, settings, client=client, registry=registry))

    assert first.success is True
    assert second.attempted is False
    assert second.skipped_reason == "Issue was already dispatched."
    assert len(client.messages) == 1
