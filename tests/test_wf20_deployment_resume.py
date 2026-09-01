import asyncio
from typing import Any

import pytest

from app.config import Settings
from app.github_events import parse_github_event
from app.hermes_dispatch import HermesEvidenceArtifact, HermesEvidenceSnapshot
from app.review_queue import ReviewWorkItemStatus, review_queue
from app.runtime_validation_review_bridge import create_runtime_validation_pending_item, enqueue_review_from_runtime_validation, enqueue_runtime_pending_item
from app.wf20_deployment_resume import (
    claim_waiting_workflow_for_request,
    is_ready_deployment_request,
    is_waiting_for_deployment_request,
    mark_waiting_workflow_failed_for_request,
    mark_waiting_workflow_resumed,
    persist_waiting_for_deployment,
)
from app.wf20_runtime_validation import AgentBusRuntimeValidationStore, RuntimeValidationState
from app.wf20_runtime_validation_safe import runtime_validation_request_from_parsed

REPO = "marcus937/jarvis-mission-control"
BRANCH = "codex-m2/wf20"
SHA = "abcdef1234567890"
PREVIEW_URL = "https://jmc-preview.vercel.app"
PUBLIC_DNS_RESULT = [(2, 1, 6, "", ("93.184.216.34", 0))]


@pytest.fixture(autouse=True)
def resolve_fake_preview_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.circuit_runtime_validation.socket.getaddrinfo",
        lambda *_args, **_kwargs: PUBLIC_DNS_RESULT,
    )


class FakeAgentBusClient:
    def __init__(self) -> None:
        self.created_work_items: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = []

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.created_work_items.append(payload)
        return {"work_item_id": "agent-bus-runtime-item-1", **payload}

    async def record_runtime_validation(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.states.append(payload)
        return {"validation_state_id": f"state-{len(self.states)}", **payload, "metadata": {"evidence_packet_id": "evidence-1"}}

    async def get_runtime_validation(self, **kwargs: Any) -> dict[str, Any]:
        return {"current_state": RuntimeValidationState.PASSED.value, "history": [{"metadata": {"status": "passed"}}], "query": kwargs}

    async def aclose(self) -> None:
        return None


class FakeGitHubClient:
    def __init__(self, *, statuses: list[dict[str, Any]] | None = None) -> None:
        self.statuses = [] if statuses is None else statuses
        self.checks: list[dict[str, Any]] = []
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str]] = []
        self.commit_statuses: list[tuple[str, str, dict[str, Any]]] = []

    async def list_commit_statuses(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.statuses

    async def list_check_runs_for_ref(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.checks

    async def list_deployments(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return []

    async def list_pull_requests_for_commit(self, repo_full_name: str, sha: str) -> list[dict[str, Any]]:
        return [
            {
                "number": 134,
                "state": "open",
                "head": {"ref": BRANCH, "sha": SHA, "repo": {"full_name": REPO}},
                "base": {"ref": "agent-integration", "repo": {"full_name": REPO}},
                "labels": [],
            }
        ]

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        self.comments.append((repo_full_name, issue_number, body))
        return {"id": 1}

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.labels.append((repo_full_name, issue_number, label))
        return {"labels": [label]}

    async def create_commit_status(self, repo_full_name: str, sha: str, **payload: Any) -> dict[str, Any]:
        self.commit_statuses.append((repo_full_name, sha, payload))
        return payload

    async def aclose(self) -> None:
        return None


class FakeHermesClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.evidence = HermesEvidenceSnapshot(
            job_id="hermes-job-1",
            manifest_fetched=True,
            bundle_fetched=True,
            final_url=f"{PREVIEW_URL}/overview",
            http_status=200,
            screenshot_present=True,
            console_error_count=0,
            network_failure_count=0,
            network_non_2xx_count=0,
            artifacts=[HermesEvidenceArtifact(file_name="screenshot.png", sha256="sha256:abc")],
        )

    async def post_runtime_validation(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"status": "PASSED", "jobId": "hermes-job-1"}

    async def collect_evidence(self, base_url: str, token: str, job_id: str, settings: Settings) -> HermesEvidenceSnapshot:
        return self.evidence

    async def aclose(self) -> None:
        return None


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def settings(**overrides: Any) -> Settings:
    data = {
        "enable_runtime_validation_review_bridge": True,
        "enable_agent_bus_dispatch": True,
        "agent_bus_base_url": "https://agent-bus.test",
        "agent_bus_token": "agent-token",
        "agent_bus_runtime_validation_token": "runtime-token",
        "enable_github_writeback": True,
        "github_token": "github-token",
        "hermes_m2_enable_dispatch": True,
        "hermes_m2_base_url": "https://hermes.test",
        "hermes_m2_token": "hermes-token",
        "hermes_default_target": PREVIEW_URL,
    }
    data.update(overrides)
    return Settings(**data)


def make_store(agent_bus: FakeAgentBusClient, github: FakeGitHubClient, hermes: FakeHermesClient) -> AgentBusRuntimeValidationStore:
    return AgentBusRuntimeValidationStore(
        hermes_client_factory=lambda: hermes,
        agent_bus_client_factory=lambda _settings: agent_bus,
        github_client_factory=lambda _settings: github,
    )


def pr_payload(*, preview_url: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
    if preview_url:
        payload["pull_request"]["deployment_url"] = preview_url
    return payload


def deployment_status_payload(*, state: str = "success", target_url: str | None = PREVIEW_URL) -> dict[str, Any]:
    deployment_status: dict[str, Any] = {
        "id": 222,
        "state": state,
        "environment": "Preview",
        "created_at": "2026-06-25T17:00:00Z",
    }
    if target_url:
        deployment_status["target_url"] = target_url
        deployment_status["environment_url"] = target_url
    return {
        "action": state,
        "repository": {"full_name": REPO},
        "sender": {"login": "vercel"},
        "deployment": {"id": 111, "ref": BRANCH, "sha": SHA, "environment": "Preview"},
        "deployment_status": deployment_status,
    }


def create_waiting_item() -> tuple[Any, Any]:
    review_queue.reset()
    parsed = parse_github_event("pull_request", pr_payload())
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=FakeGitHubClient(statuses=[])))
    item = enqueue_runtime_pending_item(create_runtime_validation_pending_item(parsed))
    item = persist_waiting_for_deployment(request, item)
    return request, item


def test_initial_pr_without_ready_vercel_deployment_persists_waiting_without_hermes_or_bb2() -> None:
    request, item = create_waiting_item()
    hermes = FakeHermesClient()

    assert is_waiting_for_deployment_request(request) is True
    assert hermes.payloads == []
    assert item.status == ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING
    assert item.runtime_validation_status == "waiting_for_deployment"
    assert item.runtime_validation_context["source"] == "wf20_waiting_for_deployment"
    assert item.runtime_validation_context["workflow_id"] == request.workflow_id


def test_ready_deployment_status_resumes_same_workflow_and_dispatches_hermes_with_verified_url() -> None:
    _, waiting_item = create_waiting_item()
    parsed = parse_github_event("deployment_status", deployment_status_payload())
    github = FakeGitHubClient(statuses=[])
    agent_bus = FakeAgentBusClient()
    hermes = FakeHermesClient()
    store = make_store(agent_bus, github, hermes)
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=github))

    assert is_ready_deployment_request(request) is True
    resumed_item = claim_waiting_workflow_for_request(request, parsed)
    assert resumed_item is not None
    assert resumed_item.id == waiting_item.id
    waiting_context = resumed_item.runtime_validation_context
    request.workflow_id = str(waiting_context["workflow_id"])
    request.correlation_id = str(waiting_context["correlation_id"])

    result = run(store.trigger(request, settings()))
    resumed_item = mark_waiting_workflow_resumed(resumed_item)
    review_item = enqueue_review_from_runtime_validation(result, settings(), existing_item=resumed_item)

    assert result.status == "completed"
    assert hermes.payloads[0]["payload"]["targetUrl"] == PREVIEW_URL
    assert review_item is not None
    assert review_item.id == waiting_item.id
    assert review_item.status == ReviewWorkItemStatus.PENDING_REVIEW


def test_duplicate_ready_deployment_status_does_not_dispatch_hermes_twice() -> None:
    _, _waiting_item = create_waiting_item()
    parsed = parse_github_event("deployment_status", deployment_status_payload())
    github = FakeGitHubClient(statuses=[])
    agent_bus = FakeAgentBusClient()
    hermes = FakeHermesClient()
    store = make_store(agent_bus, github, hermes)
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=github))

    resumed_item = claim_waiting_workflow_for_request(request, parsed)
    assert resumed_item is not None
    result = run(store.trigger(request, settings()))
    mark_waiting_workflow_resumed(resumed_item)
    enqueue_review_from_runtime_validation(result, settings(), existing_item=resumed_item)

    duplicate = claim_waiting_workflow_for_request(request, parsed)

    assert duplicate is None
    assert len(hermes.payloads) == 1


def test_failed_vercel_deployment_blocks_without_dispatching_hermes() -> None:
    _, waiting_item = create_waiting_item()
    parsed = parse_github_event("deployment_status", deployment_status_payload(state="failure", target_url="https://vercel.com/deployments/1"))
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=FakeGitHubClient(statuses=[])))
    hermes = FakeHermesClient()

    blocked_item = mark_waiting_workflow_failed_for_request(request, parsed)

    assert blocked_item is not None
    assert blocked_item.id == waiting_item.id
    assert blocked_item.status == ReviewWorkItemStatus.BLOCKED
    assert blocked_item.runtime_validation_status == "deployment_failed"
    assert hermes.payloads == []


def test_existing_happy_path_with_immediate_verified_url_still_dispatches_hermes() -> None:
    review_queue.reset()
    parsed = parse_github_event("pull_request", pr_payload(preview_url=PREVIEW_URL))
    github = FakeGitHubClient(statuses=[{"context": "Vercel", "state": "success", "target_url": PREVIEW_URL}])
    agent_bus = FakeAgentBusClient()
    hermes = FakeHermesClient()
    store = make_store(agent_bus, github, hermes)
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=github))

    result = run(store.trigger(request, settings()))

    assert result.status == "completed"
    assert hermes.payloads[0]["payload"]["targetUrl"] == PREVIEW_URL
