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

REPO = "marcus937/jarvis-mission-control"
BRANCH = "codex-m2/wf21-resume"
SHA = "abcdef1234567890abcdef1234567890abcdef12"
PREVIEW_URL = "https://jarvis-mission-control-git-wf21-resume-marcus937.vercel.app"


class FakeHermesClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.closed = False

    async def post_runtime_validation(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"status": "PASSED", "jobId": "hermes-job-wf21"}

    async def collect_evidence(self, base_url: str, token: str, job_id: str, settings: Settings) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


class FakeGitHubClient:
    def __init__(self) -> None:
        self.statuses: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.deployments: list[dict[str, Any]] = []
        self.deployment_statuses: dict[int, list[dict[str, Any]]] = {}
        self.pulls: list[dict[str, Any]] = []

    async def list_commit_statuses(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.statuses

    async def list_check_runs_for_ref(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.checks

    async def list_deployments(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.deployments

    async def list_deployment_statuses(self, repo_full_name: str, deployment_id: int) -> list[dict[str, Any]]:
        return self.deployment_statuses.get(deployment_id, [])

    async def list_pull_requests_for_commit(self, repo_full_name: str, sha: str) -> list[dict[str, Any]]:
        return self.pulls


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def settings(**overrides: Any) -> Settings:
    data = {
        "enable_runtime_validation_review_bridge": True,
        "enable_agent_bus_dispatch": False,
        "enable_github_writeback": False,
        "hermes_m2_enable_dispatch": True,
        "hermes_m2_base_url": "https://hermes.example.test",
        "hermes_m2_token": "hermes-token",
        "hermes_default_target": PREVIEW_URL,
    }
    data.update(overrides)
    return Settings(**data)


def pending_request() -> RuntimeValidationRequest:
    request = RuntimeValidationRequest(
        repo=REPO,
        pr_number=134,
        branch=BRANCH,
        base_branch="agent-integration",
        target_url=None,
        target_url_source="vercel_preview_pending",
        target_url_pending_reason="Timed out waiting for verified Vercel preview deployment readiness.",
        validation_type="playwright",
        requested_by="orchestrator_wf20",
        correlation_id="wf21-correlation",
        workflow_id="wf20-marcus937-jarvis-mission-control-pr-134-abcdef123456",
        work_item_id="work-item-134",
    )
    object.__setattr__(request, "commit_sha", SHA)
    return request


def ready_request() -> RuntimeValidationRequest:
    request = pending_request().model_copy(
        update={
            "target_url": PREVIEW_URL,
            "target_url_source": "github_verified_deployment_status_preview_url",
            "target_url_pending_reason": None,
        }
    )
    object.__setattr__(request, "commit_sha", SHA)
    return request


def pull_request_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"full_name": REPO},
        "sender": {"login": "codex"},
        "pull_request": {
            "number": 134,
            "head": {"ref": BRANCH, "sha": SHA, "repo": {"full_name": REPO}},
            "base": {"ref": "agent-integration", "repo": {"full_name": REPO}},
            "labels": [],
        },
    }


def deployment_status_payload(*, state: str = "success", branch: str = BRANCH, sha: str = SHA, repo: str = REPO) -> dict[str, Any]:
    return {
        "action": state,
        "repository": {"full_name": repo},
        "sender": {"login": "vercel"},
        "deployment": {
            "id": 9001,
            "ref": branch,
            "sha": sha,
            "environment": "Preview",
            "target_url": PREVIEW_URL,
        },
        "deployment_status": {
            "id": 42,
            "state": state,
            "environment": "Preview",
            "target_url": PREVIEW_URL,
        },
    }


def pull_request_record() -> dict[str, Any]:
    return {
        "number": 134,
        "state": "open",
        "head": {"ref": BRANCH, "sha": SHA, "repo": {"full_name": REPO}},
        "base": {"ref": "agent-integration", "repo": {"full_name": REPO}},
        "labels": [],
    }


def test_pending_then_ready_deployment_resume_dispatches_hermes_once(monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "_dns_resolution_blocker", lambda host: None)
    hermes = FakeHermesClient()
    store = AgentBusRuntimeValidationStore(hermes_client_factory=lambda: hermes)

    waiting = run(store.trigger(pending_request(), settings()))
    first_ready = run(store.trigger(ready_request(), settings()))
    duplicate_ready = run(store.trigger(ready_request(), settings()))

    assert waiting.status == "pending"
    assert first_ready.status == "completed"
    assert duplicate_ready.validation_id == first_ready.validation_id
    assert len(hermes.payloads) == 1
    assert hermes.payloads[0]["targetUrl"] == PREVIEW_URL
    assert hermes.payloads[0]["payload"]["workflow_id"] == ready_request().workflow_id


def test_deployment_status_request_builder_hydrates_pr_and_verified_preview_url() -> None:
    parsed = parse_github_event("deployment_status", deployment_status_payload())
    github = FakeGitHubClient()
    github.pulls = [pull_request_record()]

    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=github))

    assert request.pr_number == 134
    assert request.branch == BRANCH
    assert request.target_url == PREVIEW_URL
    assert request.target_url_source == "github_verified_webhook_payload_preview_url"
    assert getattr(request, "commit_sha") == SHA


def test_review_queue_dedupes_ready_deployment_status_to_waiting_workflow_by_commit_sha() -> None:
    review_queue.reset()
    waiting = enqueue_runtime_pending_item(create_runtime_validation_pending_item(parse_github_event("pull_request", pull_request_payload())))
    resumed = enqueue_runtime_pending_item(
        create_runtime_validation_pending_item(
            parse_github_event("deployment_status", deployment_status_payload()).model_copy(update={"pull_request_number": 134})
        )
    )

    assert waiting.status == ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING
    assert resumed.id == waiting.id
    assert len(review_queue.list_items()) == 1


def test_review_queue_keeps_unrelated_branch_waiting_workflow_separate() -> None:
    review_queue.reset()
    waiting = enqueue_runtime_pending_item(create_runtime_validation_pending_item(parse_github_event("pull_request", pull_request_payload())))
    other = enqueue_runtime_pending_item(
        create_runtime_validation_pending_item(
            parse_github_event(
                "deployment_status",
                deployment_status_payload(branch="codex-m2/unrelated", sha="ffffef1234567890abcdef1234567890abcdef12"),
            ).model_copy(update={"pull_request_number": 134})
        )
    )

    assert other.id != waiting.id
    assert len(review_queue.list_items()) == 2
