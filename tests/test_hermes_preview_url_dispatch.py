import asyncio
from typing import Any

from app.config import Settings
from app.github_events import parse_github_event
from app.hermes_dispatch import InMemoryHermesDispatchRegistry, dispatch_hermes_runtime_validation


class FakeGitHubClient:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str]] = []
        self.statuses = [
            {
                "context": "Vercel",
                "state": "success",
                "target_url": "https://riseos-agent-orchestrator-git-agent-integration-marcus937.vercel.app",
            }
        ]
        self.check_runs: list[dict[str, Any]] = []

    async def list_commit_statuses(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.statuses

    async def list_check_runs_for_ref(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.check_runs

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        self.comments.append((repo_full_name, issue_number, body))
        return {"id": 1}

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.labels.append((repo_full_name, issue_number, label))
        return {"labels": [label]}


class FakeHermesClient:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, str, dict[str, Any]]] = []

    async def post_job(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.jobs.append((base_url, token, payload))
        return {"status": "PASSED", "jobId": "preview-job-123"}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def settings(**overrides: Any) -> Settings:
    base = {
        "slack_channel": "#jarvis-agent-orchestrator",
        "enable_github_writeback": True,
        "hermes_m2_base_url": "http://100.70.83.13:8787",
        "hermes_m2_token": "secret-token",
        "hermes_m2_enable_dispatch": True,
        "hermes_default_target": "https://apple.com",
    }
    base.update(overrides)
    return Settings(**base)


def pr_payload() -> dict[str, Any]:
    return {
        "action": "labeled",
        "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
        "sender": {"login": "marcus"},
        "label": {"name": "playwright"},
        "pull_request": {
            "number": 77,
            "head": {
                "ref": "agent-integration",
                "sha": "abcdef1234567890",
                "repo": {"full_name": "marcus937/riseos-agent-orchestrator"},
            },
            "base": {
                "ref": "main",
                "repo": {"full_name": "marcus937/riseos-agent-orchestrator"},
            },
            "labels": [
                {"name": "runtime-agent"},
                {"name": "playwright"},
                {"name": "bb-review-needed"},
            ],
        },
    }


def test_pr_dispatch_prefers_vercel_preview_url_over_default_target() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(),
            github_client=github,
            hermes_client=hermes,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    target_url = "https://riseos-agent-orchestrator-git-agent-integration-marcus937.vercel.app"
    assert result.success is True
    assert result.target_url == target_url
    assert result.preview_url == target_url
    assert result.target_source == "github_commit_preview_url"
    assert hermes.jobs[0][2]["targetUrl"] == target_url
    assert hermes.jobs[0][2]["preview_url"] == target_url
    assert hermes.jobs[0][2]["payload"]["targetUrl"] == target_url
    assert hermes.jobs[0][2]["payload"]["previewUrl"] == target_url
    assert hermes.jobs[0][2]["payload"]["preview_url"] == target_url
    assert hermes.jobs[0][2]["payload"]["validation_type"] == "playwright"
    assert "https://apple.com" not in str(hermes.jobs[0][2])
    assert target_url in github.comments[0][2]


def test_pr_dispatch_falls_back_to_default_when_preview_url_is_absent() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()
    github.statuses = []
    hermes = FakeHermesClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(),
            github_client=github,
            hermes_client=hermes,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.success is True
    assert result.target_url == "https://apple.com"
    assert result.preview_url is None
    assert result.target_source == "hermes_default_target"
    assert hermes.jobs[0][2]["targetUrl"] == "https://apple.com"
    assert hermes.jobs[0][2]["preview_url"] is None
