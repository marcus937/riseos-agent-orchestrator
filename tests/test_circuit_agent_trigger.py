import asyncio
import logging
from typing import Any

from app.circuit_agent_trigger import wake_circuit_agent_for_work
from app.config import Settings
from app.task_dispatch import dispatch_next_agent_task


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeCircuitTriggerClient:
    def __init__(self, *, status_code: int = 202, error: Exception | None = None) -> None:
        self.status_code = status_code
        self.error = error
        self.calls: list[dict[str, str]] = []

    async def post_wakeup(self, *, url: str, token: str, message: str) -> int:
        self.calls.append({"url": url, "token": token, "message": message})
        if self.error:
            raise self.error
        return self.status_code


def test_circuit_owned_work_posts_trigger() -> None:
    client = FakeCircuitTriggerClient(status_code=204)
    settings = Settings(
        circuit_agent_trigger_url="https://agent.example/trigger",
        circuit_agent_access_token="secret-token",
    )

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
    assert result.status_code == 204
    assert client.calls == [
        {
            "url": "https://agent.example/trigger",
            "token": "secret-token",
            "message": result.message,
        }
    ]
    assert "check the Agent Bus MCP inbox/queue" in result.message
    assert "Assigned GitHub issue: #42" in result.message


def test_missing_trigger_config_skips_without_crashing() -> None:
    client = FakeCircuitTriggerClient()

    result = run(wake_circuit_agent_for_work(Settings(), target_agent="circuit", client=client))

    assert result.attempted is False
    assert result.success is False
    assert result.skipped_reason == "Circuit agent trigger is not configured."
    assert client.calls == []


def test_api_failure_is_logged_and_non_fatal(caplog: Any) -> None:
    client = FakeCircuitTriggerClient(error=RuntimeError("boom secret-token"))
    settings = Settings(
        circuit_agent_trigger_url="https://agent.example/trigger",
        circuit_agent_access_token="secret-token",
    )

    with caplog.at_level(logging.WARNING, logger="riseos_agent_orchestrator"):
        result = run(wake_circuit_agent_for_work(settings, owner_agent="circuit", client=client))

    assert result.attempted is True
    assert result.success is False
    assert result.error == "boom [REDACTED]"
    assert "circuit_agent_wakeup_failed" in caplog.text
    assert "secret-token" not in caplog.text


def test_non_circuit_work_is_noop() -> None:
    client = FakeCircuitTriggerClient()
    settings = Settings(
        circuit_agent_trigger_url="https://agent.example/trigger",
        circuit_agent_access_token="secret-token",
    )

    result = run(wake_circuit_agent_for_work(settings, target_agent="bb2", owner_agent="hermes", client=client))

    assert result.attempted is False
    assert result.success is False
    assert result.skipped_reason == "Work is not owned by Circuit."
    assert client.calls == []


class FakeTaskDispatchClient:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str]] = []

    async def list_open_issues(
        self,
        repo_full_name: str,
        *,
        labels: list[str] | None = None,
        sort: str = "created",
        direction: str = "asc",
    ) -> list[dict[str, Any]]:
        return [
            {
                "number": 42,
                "title": "Wake Circuit",
                "body": "Wire agent wake-up.",
                "created_at": "2026-06-01T00:00:00Z",
                "labels": [{"name": "agent-task"}, {"name": "agent-ready"}],
            }
        ]

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        self.comments.append((repo_full_name, issue_number, body))
        return {"id": 1}

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.labels.append((repo_full_name, issue_number, label))
        return {"labels": [label]}


def test_task_dispatch_wakes_circuit_after_assignment() -> None:
    task_client = FakeTaskDispatchClient()
    trigger_client = FakeCircuitTriggerClient()
    settings = Settings(
        circuit_agent_trigger_url="https://agent.example/trigger",
        circuit_agent_access_token="secret-token",
    )

    result = run(
        dispatch_next_agent_task(
            "marcus937/riseos-agent-orchestrator",
            task_client,
            enabled=True,
            settings=settings,
            circuit_trigger_client=trigger_client,
        )
    )

    assert result.success is True
    assert result.issue_number == 42
    assert result.circuit_wakeup_attempted is True
    assert result.circuit_wakeup_success is True
    assert task_client.comments[0][0:2] == ("marcus937/riseos-agent-orchestrator", 42)
    assert trigger_client.calls[0]["url"] == "https://agent.example/trigger"
    assert trigger_client.calls[0]["token"] == "secret-token"
    assert "check the Agent Bus MCP inbox/queue" in trigger_client.calls[0]["message"]
