import asyncio
from typing import Any

import app.circuit_runtime_validation as runtime_module
from app.circuit_runtime_validation import RuntimeValidationRequest
from app.config import Settings
from app.github_events import parse_github_event
from app.review_queue import ReviewWorkItemStatus, review_queue
from app.runtime_validation_review_bridge import create_runtime_validation_pending_item, enqueue_runtime_pending_item
from app.wf20_runtime_validation import AgentBusRuntimeValidationStore
from app.wf20_runtime_validation_safe import runtime_validation_request_from_parsed


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


class FakeHermesClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post_runtime_validation(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.posts.append(payload)
        return {"status": "PASSED", "jobId": f"job-{len(self.posts)}"}

    async def collect_evidence(self, base_url: str, token: str, job_id: str, settings: Settings) -> None:
        return None

    async def aclose(self) -> None:
        return None


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def settings() -> Settings:
    return Settings(
        enable_runtime_validation_review_bridge=True,
        enable_agent_bus_dispatch=False,
        enable_github_writeback=False,
        hermes_m2_enable_dispatch=True,
        hermes_m2_base_url="https://hermes.example.test",
        hermes_m2_token="test-token",
    )


def pull_request_payload(*, branch: str = "codex-m2/happy-path", sha: str = "abc123def4567890", pr_number: int = 137) -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"full_name": "marcus937/jarvis-mission-control"},
        "sender": {"login": "marcus"},
        "pull_request": {
            "number": pr_number,
            "head": {
                "ref": branch,
                "sha": sha,
                "repo": {"full_name": "marcus937/jarvis-mission-control"},
            },
            "base": {
                "ref": "agent-integration",
                "repo": {"full_name": "marcus937/jarvis-mission-control"},
            },
            "labels": [],
        },
    }


def deployment_status_payload(
    target_url: str,
    *,
    repo: str = "marcus937/jarvis-mission-control",
    branch: str = "codex-m2/happy-path",
    sha: str = "abc123def4567890",
    state: str = "success",
    deployment_id: int = 9001,
    deployment_status_id: int = 42,
) -> dict[str, Any]:
    return {
        "action": state,
        "repository": {"full_name": repo},
        "sender": {"login": "vercel"},
        "deployment": {
            "id": deployment_id,
            "sha": sha,
            "ref": branch,
            "environment": "Preview",
        },
        "deployment_status": {
            "id": deployment_status_id,
            "state": state,
            "environment": "Preview",
            "environment_url": target_url,
            "target_url": target_url,
        },
    }


def pull_request_record(*, branch: str = "codex-m2/happy-path", sha: str = "abc123def4567890", pr_number: int = 137) -> dict[str, Any]:
    return {
        "number": pr_number,
        "state": "open",
        "head": {
            "ref": branch,
            "sha": sha,
            "repo": {"full_name": "marcus937/jarvis-mission-control"},
        },
        "base": {
            "ref": "agent-integration",
            "repo": {"full_name": "marcus937/jarvis-mission-control"},
        },
        "labels": [],
    }


def waiting_request() -> RuntimeValidationRequest:
    request = RuntimeValidationRequest(
        repo="marcus937/jarvis-mission-control",
        pr_number=137,
        branch="codex-m2/happy-path",
        base_branch="agent-integration",
        target_url=None,
        target_url_source="vercel_preview_pending",
        target_url_pending_reason="Timed out waiting for verified Vercel preview deployment readiness.",
        validation_type="playwright",
        requested_by="orchestrator_wf20",
        correlation_id="wf21-correlation",
        workflow_id="wf20-marcus937-jarvis-mission-control-pr-137-abc123def456",
        work_item_id="work-item-137",
    )
    object.__setattr__(request, "commit_sha", "abc123def4567890")
    return request


def ready_request() -> RuntimeValidationRequest:
    target_url = "https://jarvis-mission-control-git-codex-m2-happy-path-marcus937.vercel.app"
    request = waiting_request().model_copy(
        update={
            "target_url": target_url,
            "target_url_source": "github_verified_deployment_status_preview_url",
            "target_url_pending_reason": None,
        }
    )
    object.__setattr__(request, "commit_sha", "abc123def4567890")
    return request


def test_waiting_deployment_request_resumes_with_ready_url_once(monkeypatch: Any) -> None:
    monkeypatch.setattr(runtime_module, "_dns_resolution_blocker", lambda host: None)
    hermes = FakeHermesClient()
    store = AgentBusRuntimeValidationStore(hermes_client_factory=lambda: hermes)

    waiting = run(store.trigger(waiting_request(), settings()))
    first_ready = run(store.trigger(ready_request(), settings()))
    duplicate_ready = run(store.trigger(ready_request(), settings()))

    assert waiting.status == "pending"
    assert first_ready.status == "completed"
    assert duplicate_ready.validation_id == first_ready.validation_id
    assert len(hermes.posts) == 1
    assert hermes.posts[0]["targetUrl"] == ready_request().target_url


def test_duplicate_ready_deployment_status_does_not_launch_hermes_twice(monkeypatch: Any) -> None:
    monkeypatch.setattr(runtime_module, "_dns_resolution_blocker", lambda host: None)
    hermes = FakeHermesClient()
    store = AgentBusRuntimeValidationStore(hermes_client_factory=lambda: hermes)
    github = FakeGitHubClient()
    github.pulls = [pull_request_record()]
    target_url = "https://jarvis-mission-control-git-codex-m2-happy-path-marcus937.vercel.app"
    parsed = parse_github_event("deployment_status", deployment_status_payload(target_url))
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=github))

    first = run(store.trigger(request, settings()))
    second = run(store.trigger(request, settings()))

    assert first.validation_id == second.validation_id
    assert len(hermes.posts) == 1


def test_deployment_status_dedupes_to_waiting_workflow_by_commit_sha() -> None:
    review_queue.reset()
    waiting = enqueue_runtime_pending_item(create_runtime_validation_pending_item(parse_github_event("pull_request", pull_request_payload())))
    resumed = enqueue_runtime_pending_item(
        create_runtime_validation_pending_item(
            parse_github_event(
                "deployment_status",
                deployment_status_payload("https://jarvis-mission-control-git-codex-m2-happy-path-marcus937.vercel.app"),
            )
        )
    )

    assert waiting.status == ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING
    assert resumed.id == waiting.id


def test_unrelated_branch_does_not_dedupe_to_waiting_workflow() -> None:
    review_queue.reset()
    waiting = enqueue_runtime_pending_item(create_runtime_validation_pending_item(parse_github_event("pull_request", pull_request_payload())))
    other = enqueue_runtime_pending_item(
        create_runtime_validation_pending_item(
            parse_github_event(
                "deployment_status",
                deployment_status_payload(
                    "https://jarvis-mission-control-git-other-branch-marcus937.vercel.app",
                    branch="codex-m2/other-branch",
                    sha="fff999def4567890",
                ),
            )
        )
    )

    assert other.id != waiting.id
