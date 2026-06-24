import asyncio
import logging
from typing import Any

import httpx

from app.circuit_agent_trigger import CircuitAgentTriggerHTTPClient, CircuitAgentTriggerResponse, wake_circuit_agent_for_work
from app.config import Settings


SAFE_WAKEUP_MESSAGE = (
    "Circuit Forge wake up and check your Agent Bus inbox. Only work on an explicit Agent Bus assigned work item. "
    "If no assigned work item exists, report idle and stop. Do not search GitHub issues independently unless the "
    "assigned work item explicitly instructs you to do so."
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeCircuitTriggerClient:
    def __init__(self, *, status_code: int = 202, text: str = "", error: Exception | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.error = error
        self.calls: list[dict[str, str]] = []

    async def post_wakeup(self, *, url: str, token: str, message: str) -> CircuitAgentTriggerResponse:
        self.calls.append({"url": url, "token": token, "message": message})
        if self.error:
            raise self.error
        return CircuitAgentTriggerResponse(status_code=self.status_code, text=self.text)


def test_http_client_uses_input_payload_not_message() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["content_type"] = request.headers.get("content-type")
        captured["json"] = request.read().decode("utf-8")
        return httpx.Response(202, text="accepted")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = CircuitAgentTriggerHTTPClient(http_client=http_client)

    response = run(client.post_wakeup(url="https://agent.example/trigger", token="secret-token", message="wake up"))
    run(client.aclose())

    assert response.status_code == 202
    assert captured["authorization"] == "Bearer secret-token"
    assert captured["content_type"] == "application/json"
    assert captured["json"] == '{"input":"wake up"}'
    assert "message" not in captured["json"]


def test_default_message_prevents_independent_github_issue_hunting() -> None:
    client = FakeCircuitTriggerClient(status_code=202)
    settings = Settings(
        circuit_agent_trigger_url="https://api.chatgpt.com/v1/workspace_agents/agent-id/trigger",
        circuit_agent_access_token="secret-token",
    )

    result = run(wake_circuit_agent_for_work(settings, target_agent="circuit", client=client))

    assert result.success is True
    assert result.message is not None
    assert result.message.startswith(SAFE_WAKEUP_MESSAGE)
    assert "Only work on an explicit Agent Bus assigned work item" in result.message
    assert "report idle and stop" in result.message
    assert "Do not search GitHub issues independently" in result.message
    assert client.calls[0]["message"] == result.message


def test_wakeup_message_includes_workflow_and_work_item_context() -> None:
    client = FakeCircuitTriggerClient(status_code=202)
    settings = Settings(
        circuit_agent_trigger_url="https://api.chatgpt.com/v1/workspace_agents/agent-id/trigger",
        circuit_agent_access_token="secret-token",
    )

    result = run(
        wake_circuit_agent_for_work(
            settings,
            target_agent="circuit-forge",
            repo_full_name="marcus937/riseos-agent-orchestrator",
            workflow_id="wf-test-123",
            work_item_id="work-item-123",
            client=client,
        )
    )

    assert result.success is True
    assert result.message is not None
    assert "Repository: marcus937/riseos-agent-orchestrator." in result.message
    assert "Workflow ID: wf-test-123." in result.message
    assert "Work item ID: work-item-123." in result.message
    assert client.calls[0]["message"] == result.message


def test_202_and_200_mark_success(caplog: Any) -> None:
    settings = Settings(
        circuit_agent_trigger_url="https://agent.example/api/trigger?token=do-not-log",
        circuit_agent_access_token="secret-token",
    )

    for status_code in (200, 202):
        client = FakeCircuitTriggerClient(status_code=status_code)
        with caplog.at_level(logging.INFO, logger="riseos_agent_orchestrator"):
            result = run(
                wake_circuit_agent_for_work(
                    settings,
                    target_agent="circuit-forge",
                    repo_full_name="marcus937/riseos-agent-orchestrator",
                    issue_number=42,
                    client=client,
                )
            )

        assert result.attempted is True
        assert result.success is True
        assert result.status_code == status_code
        assert "circuit_agent_wakeup_attempted" in caplog.text
        assert f'"status_code": {status_code}' in caplog.text

    assert "secret-token" not in caplog.text
    assert "do-not-log" not in caplog.text


def test_400_401_403_log_response_body_and_return_failure(caplog: Any) -> None:
    settings = Settings(
        circuit_agent_trigger_url="https://agent.example/api/trigger",
        circuit_agent_access_token="secret-token",
    )

    for status_code in (400, 401, 403):
        body = f'{{"detail":"Field required or unauthorized {status_code}"}} secret-token'
        client = FakeCircuitTriggerClient(status_code=status_code, text=body)

        with caplog.at_level(logging.INFO, logger="riseos_agent_orchestrator"):
            result = run(wake_circuit_agent_for_work(settings, owner_agent="circuit", client=client))

        assert result.attempted is True
        assert result.success is False
        assert result.status_code == status_code
        assert result.error == f"Circuit agent wakeup failed with status {status_code}."
        assert "circuit_agent_wakeup_attempted" in caplog.text
        assert "circuit_agent_wakeup_failed" in caplog.text
        assert "Field required or unauthorized" in caplog.text
        assert f'"status_code": {status_code}' in caplog.text

    assert "[REDACTED]" in caplog.text
    assert "secret-token" not in caplog.text


def test_missing_trigger_config_skips_without_crashing() -> None:
    client = FakeCircuitTriggerClient()

    result = run(wake_circuit_agent_for_work(Settings(), target_agent="circuit", client=client))

    assert result.attempted is False
    assert result.success is False
    assert result.skipped_reason == "Circuit agent trigger is not configured."
    assert client.calls == []


def test_base_workspace_agent_url_without_trigger_is_not_called() -> None:
    client = FakeCircuitTriggerClient()
    settings = Settings(
        circuit_agent_trigger_url="https://api.chatgpt.com/v1/workspace_agents/agent-id",
        circuit_agent_access_token="secret-token",
    )

    result = run(wake_circuit_agent_for_work(settings, target_agent="circuit", client=client))

    assert result.attempted is False
    assert result.success is False
    assert result.skipped_reason == "Circuit agent trigger URL must include /trigger."
    assert client.calls == []


def test_token_is_never_logged_for_exception(caplog: Any) -> None:
    client = FakeCircuitTriggerClient(error=RuntimeError("boom secret-token"))
    settings = Settings(
        circuit_agent_trigger_url="https://agent.example/secret-token/trigger",
        circuit_agent_access_token="secret-token",
    )

    with caplog.at_level(logging.INFO, logger="riseos_agent_orchestrator"):
        result = run(wake_circuit_agent_for_work(settings, owner_agent="circuit", client=client))

    assert result.attempted is True
    assert result.success is False
    assert result.error == "boom [REDACTED]"
    assert "circuit_agent_wakeup_failed" in caplog.text
    assert "secret-token" not in caplog.text
