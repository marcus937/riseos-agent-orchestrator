import asyncio
from typing import Any

from app.config import Settings
from app.github_events import parse_github_event
from app.wf20_runtime_validation import AgentBusRuntimeValidationStore
from app.wf20_runtime_validation_safe import runtime_validation_request_from_parsed


class FakeGitHubClient:
    async def list_commit_statuses(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return []

    async def list_check_runs_for_ref(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return []

    async def list_deployments(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return []


class FakeHermesClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def post_runtime_validation(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"status": "PASSED", "jobId": "hermes-job-1"}

    async def collect_evidence(self, base_url: str, token: str, job_id: str, settings: Settings) -> None:
        return None

    async def aclose(self) -> None:
        return None


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def pull_request_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"full_name": "marcus937/jarvis-mission-control"},
        "sender": {"login": "codex"},
        "pull_request": {
            "number": 134,
            "head": {
                "ref": "codex-m2/wf20",
                "sha": "abcdef1234567890",
                "repo": {"full_name": "marcus937/jarvis-mission-control"},
            },
            "base": {
                "ref": "agent-integration",
                "repo": {"full_name": "marcus937/jarvis-mission-control"},
            },
            "labels": [],
        },
    }


def test_event_driven_installer_keeps_waiting_request_pending_without_hermes() -> None:
    parsed = parse_github_event("pull_request", pull_request_payload())
    request = run(runtime_validation_request_from_parsed(parsed, Settings(), github_client=FakeGitHubClient()))
    hermes = FakeHermesClient()
    store = AgentBusRuntimeValidationStore(
        hermes_client_factory=lambda: hermes,
        agent_bus_client_factory=lambda _settings: None,
        github_client_factory=lambda _settings: None,
    )

    result = run(store.trigger(request, Settings(hermes_m2_enable_dispatch=True, hermes_m2_base_url="https://hermes.test", hermes_m2_token="token")))

    assert request.target_url is None
    assert request.target_url_source == "vercel_preview_pending"
    assert result.status == "pending"
    assert result.hermes.status == "SKIPPED"
    assert result.error == "Timed out waiting for verified Vercel preview deployment readiness."
    assert hermes.payloads == []
