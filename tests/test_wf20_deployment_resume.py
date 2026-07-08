import asyncio
from typing import Any

from app.circuit_runtime_validation import RuntimeValidationRequest
from app.config import Settings
from app.github_events import parse_github_event
from app.hermes_dispatch import HermesEvidenceArtifact, HermesEvidenceSnapshot
from app.review_queue import ReviewWorkItemStatus, review_queue
from app.runtime_validation_review_bridge import create_runtime_validation_pending_item, enqueue_review_from_runtime_validation, enqueue_runtime_pending_item
from app.storage import SQLiteStateStore
from app.wf20_deployment_resume import (
    claim_waiting_workflow_for_request,
    is_ready_deployment_request,
    is_waiting_for_deployment_request,
    list_waiting_deployment_items,
    mark_waiting_workflow_failed_for_request,
    mark_waiting_workflow_resumed,
    persist_waiting_for_deployment,
    select_waiting_workflow_for_request,
)
from app.wf20_runtime_validation import AgentBusRuntimeValidationStore, RuntimeValidationState
from app.wf20_runtime_validation_safe import runtime_validation_request_from_parsed

REPO = "marcus937/jarvis-mission-control"
BRANCH = "codex-m2/wf20"
SHA = "abcdef1234567890"
PREVIEW_URL = "https://jmc-preview.vercel.app"


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


def pr_payload(
    *,
    preview_url: str | None = None,
    branch: str = BRANCH,
    sha: str = SHA,
    pr_number: int = 134,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "opened",
        "repository": {"full_name": REPO},
        "sender": {"login": "codex"},
        "pull_request": {
            "number": pr_number,
            "head": {"ref": branch, "sha": sha, "repo": {"full_name": REPO}},
            "base": {"ref": "agent-integration", "repo": {"full_name": REPO}},
            "labels": [],
        },
    }
    if preview_url:
        payload["pull_request"]["deployment_url"] = preview_url
    return payload


def deployment_status_payload(
    *,
    state: str = "success",
    target_url: str | None = PREVIEW_URL,
    branch: str = BRANCH,
    sha: str = SHA,
    deployment_id: int = 111,
    deployment_status_id: int = 222,
) -> dict[str, Any]:
    deployment_status: dict[str, Any] = {
        "id": deployment_status_id,
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
        "deployment": {"id": deployment_id, "ref": branch, "sha": sha, "environment": "Preview"},
        "deployment_status": deployment_status,
    }


def waiting_runtime_request(
    *,
    branch: str = BRANCH,
    sha: str = SHA,
    pr_number: int = 134,
    workflow_id: str | None = None,
) -> RuntimeValidationRequest:
    workflow_id = workflow_id or f"wf20-{pr_number}-{sha[:8]}"
    request = RuntimeValidationRequest(
        repo=REPO,
        pr_number=pr_number,
        branch=branch,
        base_branch="agent-integration",
        target_url=None,
        target_url_source="vercel_preview_pending",
        target_url_pending_reason="Timed out waiting for verified Vercel preview deployment readiness.",
        validation_type="playwright",
        requested_by="orchestrator_wf20",
        correlation_id=f"correlation-{workflow_id}",
        workflow_id=workflow_id,
    )
    object.__setattr__(request, "commit_sha", sha)
    return request


def ready_runtime_request(
    *,
    branch: str = BRANCH,
    sha: str = SHA,
    pr_number: int = 134,
    workflow_id: str | None = None,
) -> RuntimeValidationRequest:
    request = waiting_runtime_request(branch=branch, sha=sha, pr_number=pr_number, workflow_id=workflow_id).model_copy(
        update={
            "target_url": PREVIEW_URL,
            "target_url_source": "github_verified_deployment_status_preview_url",
            "target_url_pending_reason": None,
        }
    )
    object.__setattr__(request, "commit_sha", sha)
    return request


def create_waiting_item() -> tuple[Any, Any]:
    review_queue.reset()
    parsed = parse_github_event("pull_request", pr_payload())
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=FakeGitHubClient(statuses=[])))
    item = enqueue_runtime_pending_item(create_runtime_validation_pending_item(parsed))
    item = persist_waiting_for_deployment(request, item)
    return request, item


def create_registry_waiting_item(request: RuntimeValidationRequest, *, storage: SQLiteStateStore | None = None) -> Any:
    parsed = parse_github_event(
        "pull_request",
        pr_payload(branch=request.branch or BRANCH, sha=getattr(request, "commit_sha", SHA), pr_number=request.pr_number or 134),
    )
    item = create_runtime_validation_pending_item(parsed)
    return persist_waiting_for_deployment(request, item, storage=storage)


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


def test_waiting_workflow_enters_registry_and_matches_ready_deployment() -> None:
    review_queue.reset()
    waiting = create_registry_waiting_item(waiting_runtime_request())
    parsed = parse_github_event("deployment_status", deployment_status_payload())
    selected = select_waiting_workflow_for_request(ready_runtime_request(), parsed)

    assert list_waiting_deployment_items() == [waiting]
    assert selected is not None
    assert selected.id == waiting.id


def test_registry_survives_unrelated_workflow_additions_and_selects_active_pr() -> None:
    review_queue.reset()
    create_registry_waiting_item(waiting_runtime_request(branch="codex-m2/old", sha="oldsha123", pr_number=120, workflow_id="old"))
    active = create_registry_waiting_item(waiting_runtime_request(branch="codex-m2/active", sha="activesha123", pr_number=183, workflow_id="active"))
    create_registry_waiting_item(waiting_runtime_request(branch="codex-m2/other", sha="othersha123", pr_number=184, workflow_id="other"))
    parsed = parse_github_event("deployment_status", deployment_status_payload(branch="codex-m2/active", sha="activesha123"))

    selected = select_waiting_workflow_for_request(
        ready_runtime_request(branch="codex-m2/active", sha="activesha123", pr_number=183, workflow_id="active"),
        parsed,
    )

    assert len(list_waiting_deployment_items()) == 3
    assert selected is not None
    assert selected.id == active.id


def test_multiple_prs_same_repository_and_branch_reuse_match_by_sha_first() -> None:
    review_queue.reset()
    create_registry_waiting_item(waiting_runtime_request(branch="codex-m2/reused", sha="sha-one", pr_number=183, workflow_id="wf-one"))
    target = create_registry_waiting_item(waiting_runtime_request(branch="codex-m2/reused", sha="sha-two", pr_number=184, workflow_id="wf-two"))
    parsed = parse_github_event("deployment_status", deployment_status_payload(branch="codex-m2/reused", sha="sha-two"))

    selected = select_waiting_workflow_for_request(
        ready_runtime_request(branch="codex-m2/reused", sha="sha-two", pr_number=184, workflow_id="wf-two"),
        parsed,
    )

    assert selected is not None
    assert selected.id == target.id


def test_identical_sha_on_different_prs_uses_pr_when_workflow_id_differs() -> None:
    review_queue.reset()
    create_registry_waiting_item(waiting_runtime_request(branch="codex-m2/one", sha="sharedsha", pr_number=183, workflow_id="wf-one"))
    target = create_registry_waiting_item(waiting_runtime_request(branch="codex-m2/two", sha="sharedsha", pr_number=184, workflow_id="wf-two"))
    parsed = parse_github_event("deployment_status", deployment_status_payload(branch="codex-m2/two", sha="sharedsha"))

    selected = select_waiting_workflow_for_request(
        ready_runtime_request(branch="codex-m2/two", sha="sharedsha", pr_number=184, workflow_id="wf-two"),
        parsed,
    )

    assert selected is not None
    assert selected.id == target.id


def test_stale_workflow_leaves_waiting_registry_after_resume() -> None:
    review_queue.reset()
    waiting = create_registry_waiting_item(waiting_runtime_request())

    mark_waiting_workflow_resumed(waiting)

    assert list_waiting_deployment_items() == []


def test_deployment_before_persistence_then_after_persistence() -> None:
    review_queue.reset()
    parsed = parse_github_event("deployment_status", deployment_status_payload())
    request = ready_runtime_request()

    assert select_waiting_workflow_for_request(request, parsed) is None
    waiting = create_registry_waiting_item(waiting_runtime_request())

    selected = select_waiting_workflow_for_request(request, parsed)
    assert selected is not None
    assert selected.id == waiting.id


def test_duplicate_deployment_webhook_and_replay_do_not_rematch_after_claim() -> None:
    review_queue.reset()
    waiting = create_registry_waiting_item(waiting_runtime_request())
    parsed = parse_github_event("deployment_status", deployment_status_payload())
    request = ready_runtime_request()

    first = claim_waiting_workflow_for_request(request, parsed)
    second = claim_waiting_workflow_for_request(request, parsed)

    assert first is not None
    assert first.id == waiting.id
    assert second is None


def test_registry_reload_after_restart_uses_persisted_waiting_items(tmp_path) -> None:
    storage = SQLiteStateStore(str(tmp_path / "orchestrator.db"))
    waiting = create_registry_waiting_item(waiting_runtime_request(), storage=storage)
    reloaded_storage = SQLiteStateStore(str(tmp_path / "orchestrator.db"))
    parsed = parse_github_event("deployment_status", deployment_status_payload())

    selected = select_waiting_workflow_for_request(ready_runtime_request(), parsed, storage=reloaded_storage)

    assert len(list_waiting_deployment_items(storage=reloaded_storage)) == 1
    assert selected is not None
    assert selected.id == waiting.id
