import asyncio
from typing import Any

from app.circuit_runtime_validation import RuntimeValidationRequest
from app.config import Settings
from app.hermes_dispatch import HermesEvidenceArtifact, HermesEvidenceSnapshot
from app.review_queue import ReviewWorkItemStatus, review_queue
from app.runtime_validation_review_bridge import enqueue_review_from_runtime_validation
from app.wf20_runtime_validation import AgentBusRuntimeValidationStore, RuntimeValidationState

REPO = "marcus937/jarvis-mission-control"
BRANCH = "codex-m2/wf20"
SHA = "abcdef1234567890"
PREVIEW_URL = "https://jmc-preview.vercel.app"
CODEX_WORK_ITEM_ID = "codex-work-item-123"
EVIDENCE_PACKET_ID = "evidence-packet-456"
WORKFLOW_ID = "wf20-marcus937-jarvis-mission-control-pr-134-abcdef123456"


class FakeAgentBusClient:
    def __init__(self) -> None:
        self.created_work_items: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = []
        self.lookup_queries: list[dict[str, Any]] = []

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.created_work_items.append(payload)
        return {"work_item_id": "new-runtime-validation-item", **payload}

    async def record_runtime_validation(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.states.append(payload)
        return {"validation_state_id": f"state-{len(self.states)}", **payload}

    async def get_runtime_validation(self, **kwargs: Any) -> dict[str, Any]:
        self.lookup_queries.append(kwargs)
        latest = self.states[-1]
        return {
            "current_state": latest["state"],
            "history": [{"metadata": latest.get("metadata", {})}],
            "query": kwargs,
        }

    async def aclose(self) -> None:
        return None


class FakeGitHubClient:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str]] = []
        self.commit_statuses: list[tuple[str, str, dict[str, Any]]] = []

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
            console_warning_count=0,
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


def runtime_request() -> RuntimeValidationRequest:
    request = RuntimeValidationRequest(
        repo=REPO,
        pr_number=134,
        branch=BRANCH,
        base_branch="agent-integration",
        target_url=PREVIEW_URL,
        target_url_source="github_verified_deployment_status_preview_url",
        validation_type="playwright",
        requested_by="agent_bus_runtime_validation",
        correlation_id=WORKFLOW_ID,
        workflow_id=WORKFLOW_ID,
        work_item_id=CODEX_WORK_ITEM_ID,
        evidence_id=EVIDENCE_PACKET_ID,
        review_dispatch={
            "work_item_id": CODEX_WORK_ITEM_ID,
            "evidence_packet_id": EVIDENCE_PACKET_ID,
            "workflow_id": WORKFLOW_ID,
            "repository": REPO,
            "pr_number": 134,
            "branch": BRANCH,
            "commit_sha": SHA,
        },
    )
    object.__setattr__(request, "commit_sha", SHA)
    object.__setattr__(request, "validation_profile", "jmc_frontend_preview_v1")
    object.__setattr__(request, "vercel_readiness", "VERCEL_READY")
    return request


def test_healthy_frontend_handoff_records_terminal_evidence_against_original_codex_work_item(monkeypatch: Any) -> None:
    review_queue.reset()
    monkeypatch.setattr(
        "app.circuit_runtime_validation.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    agent_bus = FakeAgentBusClient()
    github = FakeGitHubClient()
    hermes = FakeHermesClient()
    store = make_store(agent_bus, github, hermes)

    result = run(store.trigger(runtime_request(), settings()))
    review_item = enqueue_review_from_runtime_validation(result, settings())

    assert result.status == "completed"
    assert result.hermes.status == "PASSED"
    assert result.work_item_id == CODEX_WORK_ITEM_ID
    assert agent_bus.created_work_items == []
    assert [state["state"] for state in agent_bus.states] == [
        RuntimeValidationState.REQUESTED.value,
        RuntimeValidationState.RUNNING.value,
        RuntimeValidationState.PLAYWRIGHT_EXECUTED.value,
        RuntimeValidationState.PASSED.value,
    ]
    assert {state["work_item_id"] for state in agent_bus.states} == {CODEX_WORK_ITEM_ID}
    assert agent_bus.lookup_queries == [{"work_item_id": CODEX_WORK_ITEM_ID}]

    final_state = agent_bus.states[-1]
    metadata = final_state["metadata"]
    assert final_state["terminal_status"] == "passed"
    assert final_state["runtime_validation_id"] == result.validation_id
    assert final_state["hermes_job_id"] == "hermes-job-1"
    assert final_state["evidence_packet_id"] == EVIDENCE_PACKET_ID
    assert final_state["gate_lookup_key"]["work_item_id"] == CODEX_WORK_ITEM_ID
    assert final_state["gate_lookup_key"]["commit_sha"] == SHA
    assert metadata["status"] == "passed"
    assert metadata["runtime_validation_id"] == result.validation_id
    assert metadata["gate_lookup_key"]["work_item_id"] == CODEX_WORK_ITEM_ID
    assert metadata["final_url"] == f"{PREVIEW_URL}/overview"
    assert metadata["http_status"] == 200
    assert metadata["screenshot_artifact"] == "screenshot.png"

    assert hermes.payloads[0]["work_item_id"] == CODEX_WORK_ITEM_ID
    assert hermes.payloads[0]["payload"]["targetUrl"] == PREVIEW_URL
    assert review_item is not None
    assert review_item.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert review_item.runtime_validation_context["gate_lookup_key"]["work_item_id"] == CODEX_WORK_ITEM_ID
    assert review_item.runtime_validation_context["hermes_job_id"] == "hermes-job-1"
