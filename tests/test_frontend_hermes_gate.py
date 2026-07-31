import asyncio
from typing import Any

from app.config import Settings
from app.frontend_validation import HermesValidationState
from app.github_events import parse_github_event
from app.hermes_dispatch import InMemoryHermesDispatchRegistry, dispatch_hermes_runtime_validation


class FakeGitHubClient:
    def __init__(self, statuses: list[dict[str, Any]] | None = None) -> None:
        self.statuses = statuses or [
            {
                "context": "Vercel",
                "state": "success",
                "target_url": "https://jarvis-mission-control-git-codex-m2-wf18-hall-2382s-projects.vercel.app",
            }
        ]
        self.check_runs: list[dict[str, Any]] = []
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str]] = []
        self.status_writes: list[tuple[str, str, str, str, str]] = []

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

    async def create_commit_status(self, repo_full_name: str, sha: str, state: str, context: str, description: str) -> dict[str, Any]:
        self.status_writes.append((repo_full_name, sha, state, context, description))
        return {"state": state}


class FakeHermesClient:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {"status": "PASSED", "jobId": "hermes-job-123"}
        self.jobs: list[tuple[str, str, dict[str, Any]]] = []

    async def post_job(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.jobs.append((base_url, token, payload))
        return self.response


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def settings(**overrides: Any) -> Settings:
    base = {
        "enable_github_writeback": True,
        "hermes_m2_base_url": "http://100.70.83.13:8787",
        "hermes_m2_token": "secret-token",
        "hermes_m2_enable_dispatch": True,
        "hermes_default_target": "https://example.com",
    }
    base.update(overrides)
    return Settings(**base)


def frontend_codex_pr_payload(action: str = "opened") -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": "marcus937/jarvis-mission-control"},
        "sender": {"login": "codex-m2"},
        "pull_request": {
            "number": 125,
            "head": {
                "ref": "codex-m2/workflow-wf18",
                "sha": "25a04e9c9ae84df0e11b653d63cc401ed41858c6",
                "repo": {"full_name": "marcus937/jarvis-mission-control"},
            },
            "base": {
                "ref": "agent-integration",
                "repo": {"full_name": "marcus937/jarvis-mission-control"},
            },
            "labels": [],
        },
    }


def test_codex_m2_frontend_pr_to_agent_integration_dispatches_after_vercel_ready() -> None:
    parsed = parse_github_event("pull_request", frontend_codex_pr_payload())
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

    assert result.status == "PASSED"
    assert result.validation_state == HermesValidationState.HERMES_VALIDATION_PASSED
    assert result.validation_profile == "jmc_frontend_preview_v1"
    payload = hermes.jobs[0][2]
    assert payload["validation_profile"] == "jmc_frontend_preview_v1"
    assert payload["payload"]["validationProfile"] == "jmc_frontend_preview_v1"
    assert "overview_renders" in payload["payload"]["requiredAssertions"]
    assert github.comments[0][2].startswith("## Hermes Runtime Validation")
    assert ("marcus937/jarvis-mission-control", 125, "agent-verified") in github.labels
    assert github.status_writes[0][3] == "Hermes Playwright Validation"


def test_codex_m2_frontend_pr_with_failed_vercel_deployment_is_blocked() -> None:
    parsed = parse_github_event("pull_request", frontend_codex_pr_payload())
    github = FakeGitHubClient(
        statuses=[{"context": "Vercel", "state": "failure", "target_url": "https://vercel.com/failing-deployment"}]
    )
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

    assert result.status == "BLOCKED"
    assert result.validation_state == HermesValidationState.HERMES_VALIDATION_BLOCKED
    assert "HERMES_VALIDATION_BLOCKED" in (result.error or "")
    assert hermes.jobs == []
    assert "Status: BLOCKED" in github.comments[0][2]
    assert ("marcus937/jarvis-mission-control", 125, "agent-blocked") in github.labels


def test_backend_repo_codex_pr_does_not_auto_dispatch_without_labels() -> None:
    payload = frontend_codex_pr_payload()
    payload["repository"]["full_name"] = "marcus937/Project-Jarvis"
    payload["pull_request"]["head"]["repo"]["full_name"] = "marcus937/Project-Jarvis"
    payload["pull_request"]["base"]["repo"]["full_name"] = "marcus937/Project-Jarvis"
    parsed = parse_github_event("pull_request", payload)
    hermes = FakeHermesClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(),
            hermes_client=hermes,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.attempted is False
    assert hermes.jobs == []
