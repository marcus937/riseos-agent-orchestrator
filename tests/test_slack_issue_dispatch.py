import asyncio
import json
import logging
from typing import Any

from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings, get_settings
from app.event_store import event_store
from app.github_events import GitHubEventType, parse_github_event
from app.main import app
from app.operational_logging import log_slack_issue_dispatch_result
from app.review_queue import review_queue
from app.security import build_signature
from app.slack_issue_dispatch import (
    AGENT_READY_LABEL,
    InMemoryDispatchedIssueRegistry,
    SlackIssueDispatchResult,
    build_circuit_slack_message,
    dispatch_ready_issue_to_slack,
)


class FakeSlackClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def post_message(self, *, channel: str, text: str) -> None:
        self.messages.append((channel, text))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def signed_headers(secret: str, event: str, payload: bytes) -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": build_signature(secret, payload),
        "Content-Type": "application/json",
    }


def client_with_secret(secret: str = "test-secret") -> TestClient:
    get_settings.cache_clear()
    event_store.reset()
    review_queue.reset()
    app.dependency_overrides[get_settings] = lambda: Settings(
        github_webhook_secret=secret,
        slack_webhook_url="https://hooks.slack.test/services/test",
        slack_channel="#jarvis-agent-orchestrator",
    )
    return TestClient(app)


def issue_payload(
    *,
    action: str = "opened",
    repo: str = "marcus937/Project-Jarvis",
    issue_number: int = 11,
    state: str = "open",
    labels: list[str] | None = None,
    action_label: str | None = None,
    title: str = "Architecture plan: shared memory layer",
    issue_url: str | None = None,
) -> dict[str, Any]:
    labels = labels or [AGENT_READY_LABEL]
    issue_url = issue_url or f"https://github.com/{repo}/issues/{issue_number}"
    payload: dict[str, Any] = {
        "action": action,
        "repository": {"full_name": repo},
        "sender": {"login": "marcus"},
        "issue": {
            "number": issue_number,
            "title": title,
            "state": state,
            "html_url": issue_url,
            "labels": [{"name": label} for label in labels],
        },
    }
    if action_label is not None:
        payload["label"] = {"name": action_label}
    return payload


def test_ping_parser_accepts_hermes_repository_context() -> None:
    parsed = parse_github_event(
        "ping",
        {
            "repository": {"full_name": "marcus937/hermes-runtime-agent"},
            "sender": {"login": "marcus"},
        },
    )

    assert parsed.event_type == GitHubEventType.PING
    assert parsed.repository == "marcus937/hermes-runtime-agent"
    assert parsed.sender == "marcus"


def test_issues_parser_extracts_issue_context() -> None:
    parsed = parse_github_event("issues", issue_payload(action="labeled", action_label=AGENT_READY_LABEL))

    assert parsed.event_type == GitHubEventType.ISSUES
    assert parsed.action == "labeled"
    assert parsed.repository == "marcus937/Project-Jarvis"
    assert parsed.issue_number == 11
    assert parsed.issue_title == "Architecture plan: shared memory layer"
    assert parsed.issue_state == "open"
    assert parsed.issue_url == "https://github.com/marcus937/Project-Jarvis/issues/11"
    assert parsed.action_label == AGENT_READY_LABEL
    assert parsed.labels == [AGENT_READY_LABEL]


def test_opened_agent_ready_issue_dispatches_to_slack() -> None:
    parsed = parse_github_event("issues", issue_payload(action="opened"))
    client = FakeSlackClient()
    registry = InMemoryDispatchedIssueRegistry()

    result = run(
        dispatch_ready_issue_to_slack(
            parsed,
            Settings(slack_webhook_url="https://hooks.slack.test/services/test", slack_channel="#jarvis-agent-orchestrator"),
            client=client,
            registry=registry,
        )
    )

    assert result.attempted is True
    assert result.success is True
    assert result.issue_key == "marcus937/Project-Jarvis#11"
    assert client.messages[0][0] == "#jarvis-agent-orchestrator"
    assert "@circuit-forge" in client.messages[0][1]
    assert "Repo: marcus937/Project-Jarvis" in client.messages[0][1]
    assert "Issue: #11 - Architecture plan: shared memory layer" in client.messages[0][1]
    assert "Labels: agent-ready" in client.messages[0][1]
    assert "Branch rule: agent-integration only" in client.messages[0][1]
    assert "no merge, no deploy" in client.messages[0][1]


def test_labeled_agent_ready_issue_dispatches_to_slack() -> None:
    parsed = parse_github_event("issues", issue_payload(action="labeled", action_label=AGENT_READY_LABEL))
    client = FakeSlackClient()

    result = run(
        dispatch_ready_issue_to_slack(
            parsed,
            Settings(slack_webhook_url="https://hooks.slack.test/services/test"),
            client=client,
            registry=InMemoryDispatchedIssueRegistry(),
        )
    )

    assert result.success is True
    assert len(client.messages) == 1


def test_dispatch_allows_exact_approved_full_repo_name() -> None:
    parsed = parse_github_event("issues", issue_payload(repo="marcus937/Rylinn-Field-App-Codex"))
    client = FakeSlackClient()

    result = run(
        dispatch_ready_issue_to_slack(
            parsed,
            Settings(slack_webhook_url="https://hooks.slack.test/services/test"),
            client=client,
            registry=InMemoryDispatchedIssueRegistry(),
        )
    )

    assert result.success is True
    assert result.issue_key == "marcus937/Rylinn-Field-App-Codex#11"


def test_dispatch_allows_hermes_runtime_agent_repo() -> None:
    parsed = parse_github_event("issues", issue_payload(repo="marcus937/hermes-runtime-agent"))
    client = FakeSlackClient()

    result = run(
        dispatch_ready_issue_to_slack(
            parsed,
            Settings(slack_webhook_url="https://hooks.slack.test/services/test"),
            client=client,
            registry=InMemoryDispatchedIssueRegistry(),
        )
    )

    assert result.success is True
    assert result.issue_key == "marcus937/hermes-runtime-agent#11"
    assert "Repo: marcus937/hermes-runtime-agent" in client.messages[0][1]


def test_dispatch_ignores_closed_issues() -> None:
    parsed = parse_github_event("issues", issue_payload(state="closed"))
    client = FakeSlackClient()

    result = run(
        dispatch_ready_issue_to_slack(
            parsed,
            Settings(slack_webhook_url="https://hooks.slack.test/services/test"),
            client=client,
            registry=InMemoryDispatchedIssueRegistry(),
        )
    )

    assert result.attempted is False
    assert result.skipped_reason == "Issue is not open."
    assert client.messages == []


def test_dispatch_requires_agent_ready_label() -> None:
    parsed = parse_github_event("issues", issue_payload(labels=["bug"]))
    client = FakeSlackClient()

    result = run(
        dispatch_ready_issue_to_slack(
            parsed,
            Settings(slack_webhook_url="https://hooks.slack.test/services/test"),
            client=client,
            registry=InMemoryDispatchedIssueRegistry(),
        )
    )

    assert result.skipped_reason == "Issue does not have agent-ready label."
    assert client.messages == []


def test_dispatch_requires_exact_approved_repo_full_name() -> None:
    parsed = parse_github_event("issues", issue_payload(repo="evil/Project-Jarvis"))
    client = FakeSlackClient()

    result = run(
        dispatch_ready_issue_to_slack(
            parsed,
            Settings(slack_webhook_url="https://hooks.slack.test/services/test"),
            client=client,
            registry=InMemoryDispatchedIssueRegistry(),
        )
    )

    assert result.skipped_reason == "Repository is not approved for Circuit Slack dispatch."
    assert client.messages == []


def test_dispatch_deduplicates_issue_key() -> None:
    parsed = parse_github_event("issues", issue_payload())
    client = FakeSlackClient()
    registry = InMemoryDispatchedIssueRegistry()
    settings = Settings(slack_webhook_url="https://hooks.slack.test/services/test")

    first = run(dispatch_ready_issue_to_slack(parsed, settings, client=client, registry=registry))
    second = run(dispatch_ready_issue_to_slack(parsed, settings, client=client, registry=registry))

    assert first.success is True
    assert second.attempted is False
    assert second.skipped_reason == "Issue was already dispatched."
    assert len(client.messages) == 1


def test_message_includes_required_fields() -> None:
    parsed = parse_github_event("issues", issue_payload(labels=[AGENT_READY_LABEL, "ARCHITECT_REVIEW_REQUIRED"]))

    message = build_circuit_slack_message(parsed, channel="#jarvis-agent-orchestrator")

    assert "@circuit-forge" in message
    assert "Channel: #jarvis-agent-orchestrator" in message
    assert "Repo: marcus937/Project-Jarvis" in message
    assert "Issue: #11 - Architecture plan: shared memory layer" in message
    assert "Labels: agent-ready, ARCHITECT_REVIEW_REQUIRED" in message
    assert "URL: https://github.com/marcus937/Project-Jarvis/issues/11" in message
    assert "Branch rule: agent-integration only" in message
    assert "no merge, no deploy, and no branch mutation" in message


def test_message_sanitizes_slack_control_sequences() -> None:
    parsed = parse_github_event(
        "issues",
        issue_payload(
            title="Ping <!channel> <@U123> & <danger>",
            labels=[AGENT_READY_LABEL, "<!here>", "<@U123>"],
            issue_url="https://github.com/marcus937/Project-Jarvis/issues/11?x=<@U999>&y=<&>",
        ),
    )

    message = build_circuit_slack_message(parsed, channel="#jarvis-agent-orchestrator")

    assert "<!channel>" not in message
    assert "<!here>" not in message
    assert "<@U123>" not in message
    assert "<@U999>" not in message
    assert "&lt;!channel&gt;" in message
    assert "&lt;!here&gt;" in message
    assert "&lt;@U123&gt;" in message
    assert "&lt;@U999&gt;" in message
    assert "&amp;" in message


def test_slack_dispatch_logging_uses_structured_event_names(caplog: Any) -> None:
    parsed = parse_github_event("issues", issue_payload())
    caplog.set_level(logging.INFO, logger="riseos_agent_orchestrator")

    cases = [
        (SlackIssueDispatchResult(attempted=True, success=True, issue_key="marcus937/Project-Jarvis#11"), "slack_issue_dispatch_succeeded"),
        (SlackIssueDispatchResult(attempted=True, success=False, issue_key="marcus937/Project-Jarvis#11", error="boom"), "slack_issue_dispatch_failed"),
        (SlackIssueDispatchResult(issue_key="marcus937/Project-Jarvis#11", skipped_reason="Issue was already dispatched."), "slack_issue_dispatch_duplicate_suppressed"),
        (SlackIssueDispatchResult(issue_key="marcus937/Project-Jarvis#11", skipped_reason="Slack dispatch is not configured."), "slack_issue_dispatch_missing_config"),
        (SlackIssueDispatchResult(issue_key="evil/Project-Jarvis#11", skipped_reason="Repository is not approved for Circuit Slack dispatch."), "slack_issue_dispatch_invalid_repo"),
    ]

    for result, expected_event in cases:
        caplog.clear()
        log_slack_issue_dispatch_result(parsed, result)
        assert f'"event": "{expected_event}"' in caplog.text


def test_signed_ping_webhook_is_accepted_without_slack_dispatch(monkeypatch: Any) -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    dispatched: list[tuple[str | None, int | None]] = []

    async def fake_dispatch(parsed: Any, settings: Settings) -> SlackIssueDispatchResult:
        dispatched.append((parsed.repository, parsed.issue_number))
        return SlackIssueDispatchResult(skipped_reason="Not a GitHub issues event.")

    monkeypatch.setattr(main_module, "dispatch_ready_issue_to_slack", fake_dispatch)
    body = json.dumps({"repository": {"full_name": "marcus937/hermes-runtime-agent"}}).encode("utf-8")

    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "ping", body))

    assert response.status_code == 200
    assert response.json()["event_type"] == "ping"
    assert response.json()["repository"] == "marcus937/hermes-runtime-agent"
    assert response.json()["task_state"] == "working"
    assert dispatched == [("marcus937/hermes-runtime-agent", None)]


def test_signed_issues_webhook_invokes_slack_dispatch(monkeypatch: Any) -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    dispatched: list[tuple[str | None, int | None]] = []

    async def fake_dispatch(parsed: Any, settings: Settings) -> SlackIssueDispatchResult:
        dispatched.append((parsed.repository, parsed.issue_number))
        return SlackIssueDispatchResult(attempted=True, success=True, issue_key=f"{parsed.repository}#{parsed.issue_number}")

    monkeypatch.setattr(main_module, "dispatch_ready_issue_to_slack", fake_dispatch)
    body = json.dumps(issue_payload(repo="marcus937/hermes-runtime-agent")).encode("utf-8")

    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "issues", body))

    assert response.status_code == 200
    assert response.json()["event_type"] == "issues"
    assert response.json()["issue_number"] == 11
    assert dispatched == [("marcus937/hermes-runtime-agent", 11)]


def test_unsigned_issues_webhook_is_rejected_before_slack_dispatch(monkeypatch: Any) -> None:
    client = client_with_secret()
    dispatched: list[tuple[str | None, int | None]] = []

    async def fake_dispatch(parsed: Any, settings: Settings) -> SlackIssueDispatchResult:
        dispatched.append((parsed.repository, parsed.issue_number))
        return SlackIssueDispatchResult(attempted=True, success=True, issue_key=f"{parsed.repository}#{parsed.issue_number}")

    monkeypatch.setattr(main_module, "dispatch_ready_issue_to_slack", fake_dispatch)

    response = client.post("/webhooks/github", json=issue_payload(), headers={"X-GitHub-Event": "issues"})

    assert response.status_code == 401
    assert dispatched == []
