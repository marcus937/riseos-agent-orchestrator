from typing import Any

import anyio

from app.circuit_runtime_validation import runtime_validation_store
from app.config import Settings
from app.github_events import parse_github_event
from app.hermes_contract import runtime_validation_request_from_parsed


class FakePreviewClient:
    def __init__(self, statuses: list[dict[str, Any]] | None = None, checks: list[dict[str, Any]] | None = None) -> None:
        self.statuses = statuses or []
        self.checks = checks or []

    async def list_commit_statuses(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.statuses

    async def list_check_runs_for_ref(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.checks


class FailingHermesClient:
    async def post_runtime_validation(self, *args: object, **kwargs: object) -> dict[str, Any]:
        raise AssertionError("pending preview validation must not dispatch Hermes")

    async def collect_evidence(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("pending preview validation must not collect evidence")

    async def aclose(self) -> None:
        pass


def _settings() -> Settings:
    return Settings(
        github_webhook_secret="test-secret",
        orchestrator_admin_token="admin-token",
        enable_runtime_validation_review_bridge=True,
        hermes_m2_enable_dispatch=True,
        hermes_m2_base_url="https://hermes.example.test",
        hermes_m2_token="hermes-token",
        hermes_default_target="https://apple.com",
    )


def _payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "number": 91,
        "repository": {"full_name": "marcus937/jarvis-mission-control"},
        "pull_request": {
            "number": 91,
            "head": {"sha": "abc123", "ref": "codex-m2/workflow-wf-1", "repo": {"full_name": "marcus937/jarvis-mission-control"}},
            "base": {"ref": "agent-integration", "repo": {"full_name": "marcus937/jarvis-mission-control"}},
            "labels": [],
        },
    }


def test_runtime_validation_uses_latest_successful_vercel_preview() -> None:
    parsed = parse_github_event("pull_request", _payload())
    client = FakePreviewClient(
        statuses=[
            {
                "state": "success",
                "target_url": "https://old-preview.vercel.app",
                "updated_at": "2026-06-20T01:00:00Z",
            },
            {
                "state": "failure",
                "target_url": "https://failed-preview.vercel.app",
                "updated_at": "2026-06-20T03:00:00Z",
            },
        ],
        checks=[
            {
                "status": "completed",
                "conclusion": "success",
                "details_url": "https://new-preview.vercel.app",
                "completed_at": "2026-06-20T04:00:00Z",
            }
        ],
    )

    request = anyio.run(runtime_validation_request_from_parsed, parsed, _settings(), github_client=client)

    assert request.target_url == "https://new-preview.vercel.app"
    assert request.target_url_source == "github_commit_preview_url"
    assert request.target_url != "https://apple.com"


def test_missing_pr_preview_stays_pending_and_does_not_use_default_target(monkeypatch) -> None:
    parsed = parse_github_event("pull_request", _payload())
    request = anyio.run(runtime_validation_request_from_parsed, parsed, _settings(), github_client=FakePreviewClient())

    assert request.target_url is None
    assert request.target_url_source == "vercel_preview_pending"
    assert request.target_url_pending_reason

    monkeypatch.setattr(runtime_validation_store, "_hermes_client_factory", lambda: FailingHermesClient())
    result = anyio.run(runtime_validation_store.trigger, request, _settings())

    assert result.status == "pending"
    assert result.hermes.target_url is None
    assert result.hermes.target_source == "vercel_preview_pending"
    assert result.error == request.target_url_pending_reason
