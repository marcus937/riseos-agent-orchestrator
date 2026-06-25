import asyncio
from typing import Any

from app.config import Settings
from app.github_events import GitHubEventType, parse_github_event
from app.wf20_runtime_validation import VercelReadiness, runtime_validation_required_for_parsed
from app.wf20_runtime_validation_safe import resolve_verified_vercel_readiness, runtime_validation_request_from_parsed


class FakeGitHubClient:
    def __init__(self) -> None:
        self.statuses: list[dict[str, Any]] = []
        self.check_runs: list[dict[str, Any]] = []
        self.deployments: list[dict[str, Any]] = []
        self.deployment_statuses: dict[int, list[dict[str, Any]]] = {}
        self.pulls: list[dict[str, Any]] = []

    async def list_commit_statuses(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.statuses

    async def list_check_runs_for_ref(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.check_runs

    async def list_deployments(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.deployments

    async def list_deployment_statuses(self, repo_full_name: str, deployment_id: int) -> list[dict[str, Any]]:
        return self.deployment_statuses.get(deployment_id, [])

    async def list_pull_requests_for_commit(self, repo_full_name: str, sha: str) -> list[dict[str, Any]]:
        return self.pulls


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def pull_request_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"full_name": "marcus937/jarvis-mission-control"},
        "sender": {"login": "marcus"},
        "pull_request": {
            "number": 136,
            "head": {
                "ref": "codex-m2/fatal-react-crash",
                "sha": "abc123def4567890",
                "repo": {"full_name": "marcus937/jarvis-mission-control"},
            },
            "base": {
                "ref": "agent-integration",
                "repo": {"full_name": "marcus937/jarvis-mission-control"},
            },
            "labels": [],
        },
    }


def deployment_status_payload(target_url: str) -> dict[str, Any]:
    return {
        "action": "success",
        "repository": {"full_name": "marcus937/jarvis-mission-control"},
        "sender": {"login": "vercel"},
        "deployment": {
            "id": 9001,
            "sha": "abc123def4567890",
            "ref": "codex-m2/fatal-react-crash",
            "environment": "Preview",
            "description": "Vercel preview deployment",
        },
        "deployment_status": {
            "id": 42,
            "state": "success",
            "environment": "Preview",
            "environment_url": target_url,
            "target_url": target_url,
            "description": "Vercel deployment ready",
        },
    }


def pull_request_record() -> dict[str, Any]:
    return {
        "number": 136,
        "state": "open",
        "head": {
            "ref": "codex-m2/fatal-react-crash",
            "sha": "abc123def4567890",
            "repo": {"full_name": "marcus937/jarvis-mission-control"},
        },
        "base": {
            "ref": "agent-integration",
            "repo": {"full_name": "marcus937/jarvis-mission-control"},
        },
        "labels": [{"name": "frontend"}],
    }


def test_runtime_validation_uses_ready_deployment_status_when_statuses_and_checks_are_empty() -> None:
    parsed = parse_github_event("pull_request", pull_request_payload())
    github = FakeGitHubClient()
    target_url = "https://jarvis-mission-control-git-codex-m2-fatal-react-crash-marcus937.vercel.app"
    github.deployments = [
        {
            "id": 9001,
            "sha": "abc123def4567890",
            "ref": "codex-m2/fatal-react-crash",
            "environment": "Preview",
            "description": "Vercel preview deployment",
        }
    ]
    github.deployment_statuses = {
        9001: [
            {
                "id": 42,
                "state": "success",
                "environment": "Preview",
                "environment_url": target_url,
                "target_url": target_url,
                "description": "Vercel deployment ready",
            }
        ]
    }

    readiness, resolved_url, target_source, reason = run(resolve_verified_vercel_readiness(parsed, github))

    assert readiness == VercelReadiness.READY
    assert resolved_url == target_url
    assert target_source == "github_verified_deployment_status_preview_url"
    assert reason is None


def test_runtime_validation_request_hydrates_target_url_from_ready_deployment_status() -> None:
    parsed = parse_github_event("pull_request", pull_request_payload())
    github = FakeGitHubClient()
    target_url = "https://jarvis-mission-control-git-codex-m2-fatal-react-crash-marcus937.vercel.app"
    github.deployments = [{"id": 9001, "sha": "abc123def4567890", "ref": "codex-m2/fatal-react-crash", "environment": "Preview"}]
    github.deployment_statuses = {9001: [{"id": 42, "state": "success", "environment_url": target_url}]}

    request = run(runtime_validation_request_from_parsed(parsed, Settings(), github_client=github))

    assert request.pr_number == 136
    assert request.branch == "codex-m2/fatal-react-crash"
    assert request.base_branch == "agent-integration"
    assert request.target_url == target_url
    assert request.target_url_source == "github_verified_deployment_status_preview_url"
    assert getattr(request, "vercel_readiness") == "VERCEL_READY"


def test_deployment_status_webhook_routes_and_hydrates_pr_context() -> None:
    target_url = "https://jarvis-mission-control-git-codex-m2-fatal-react-crash-marcus937.vercel.app"
    parsed = parse_github_event("deployment_status", deployment_status_payload(target_url))
    github = FakeGitHubClient()
    github.pulls = [pull_request_record()]

    assert parsed.event_type == GitHubEventType.PULL_REQUEST
    assert parsed.pull_request_number is None
    assert runtime_validation_required_for_parsed(parsed, Settings(enable_runtime_validation_review_bridge=True), has_review_context=False) is True

    request = run(runtime_validation_request_from_parsed(parsed, Settings(), github_client=github))

    assert request.pr_number == 136
    assert request.branch == "codex-m2/fatal-react-crash"
    assert request.base_branch == "agent-integration"
    assert request.target_url == target_url
    assert request.target_url_source == "github_verified_webhook_payload_preview_url"
    assert request.workflow_id == "wf20-marcus937-jarvis-mission-control-pr-136-abc123def456"


def test_runtime_validation_only_waits_when_all_candidate_sources_lack_ready_preview() -> None:
    parsed = parse_github_event("pull_request", pull_request_payload())
    github = FakeGitHubClient()
    github.statuses = [{"context": "Vercel", "state": "pending", "target_url": "https://example.com"}]
    github.deployments = [{"id": 9001, "sha": "abc123def4567890", "ref": "codex-m2/fatal-react-crash", "environment": "Preview"}]
    github.deployment_statuses = {9001: [{"id": 42, "state": "pending", "environment": "Preview"}]}

    readiness, resolved_url, target_source, reason = run(resolve_verified_vercel_readiness(parsed, github))

    assert readiness == VercelReadiness.TIMEOUT
    assert resolved_url is None
    assert target_source == "vercel_preview_pending"
    assert reason == "Timed out waiting for verified Vercel preview deployment readiness."
