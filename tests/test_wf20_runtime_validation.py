import asyncio
from typing import Any

from app.circuit_runtime_validation import RuntimeValidationRequest
from app.config import Settings
from app.github_events import parse_github_event
from app.hermes_dispatch import HermesEvidenceArtifact, HermesEvidenceSnapshot
from app.wf20_runtime_validation import (
    AgentBusRuntimeValidationStore,
    RuntimeValidationState,
    VercelReadiness,
    frontend_validation_profile_for_repo,
    resolve_vercel_readiness,
    runtime_validation_required_for_parsed,
)
from app.wf20_runtime_validation_safe import runtime_validation_request_from_parsed


class FakeAgentBusClient:
    def __init__(self) -> None:
        self.created_work_items: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = []
        self.closed = False

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.created_work_items.append(payload)
        return {"work_item_id": "work-item-1", **payload}

    async def record_runtime_validation(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.states.append(payload)
        return {"validation_state_id": f"state-{len(self.states)}", **payload, "metadata": {**payload.get("metadata", {}), "evidence_packet_id": "evidence-1"}}

    async def get_runtime_validation(self, **kwargs: Any) -> dict[str, Any]:
        return {"current_state": "HERMES_VALIDATION_PASSED", "history": [{"metadata": {"status": "passed"}}], "query": kwargs}

    async def aclose(self) -> None:
        self.closed = True


class FakeGitHubClient:
    def __init__(self, *, statuses: list[dict[str, Any]] | None = None, checks: list[dict[str, Any]] | None = None) -> None:
        self.statuses = statuses or []
        self.checks = checks or []
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str]] = []
        self.commit_statuses: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    async def list_commit_statuses(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.statuses

    async def list_check_runs_for_ref(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.checks

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
        self.closed = True


class FakeHermesClient:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {"status": "PASSED", "jobId": "hermes-job-1"}
        self.payloads: list[dict[str, Any]] = []
        self.closed = False

    async def post_runtime_validation(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return self.response

    async def collect_evidence(self, base_url: str, token: str, job_id: str, settings: Settings) -> HermesEvidenceSnapshot:
        return HermesEvidenceSnapshot(
            job_id=job_id,
            manifest_fetched=True,
            bundle_fetched=True,
            final_url="https://jmc-preview.vercel.app/overview",
            http_status=200,
            screenshot_present=True,
            console_error_count=0,
            network_failure_count=0,
            network_non_2xx_count=0,
            artifacts=[HermesEvidenceArtifact(file_name="screenshot.png", sha256="sha256:abc")],
        )

    async def aclose(self) -> None:
        self.closed = True


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def settings(**overrides: Any) -> Settings:
    data = {
        "enable_runtime_validation_review_bridge": True,
        "enable_agent_bus_dispatch": True,
        "agent_bus_base_url": "https://agent-bus.test",
        "agent_bus_token": "agent-token",
        "enable_github_writeback": True,
        "github_token": "github-token",
        "hermes_m2_enable_dispatch": True,
        "hermes_m2_base_url": "https://hermes.test",
        "hermes_m2_token": "hermes-token",
        "hermes_default_target": "https://jmc-preview.vercel.app",
    }
    data.update(overrides)
    return Settings(**data)


def pr_payload(*, repo: str = "marcus937/jarvis-mission-control", action: str = "opened", labels: list[str] | None = None, preview_url: str | None = "https://jmc-preview.vercel.app", base_ref: str = "agent-integration") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "repository": {"full_name": repo},
        "sender": {"login": "codex"},
        "pull_request": {
            "number": 134,
            "head": {"ref": "codex-m2/wf20", "sha": "abcdef1234567890", "repo": {"full_name": repo}},
            "base": {"ref": base_ref, "repo": {"full_name": repo}},
            "labels": [{"name": item} for item in (labels or [])],
        },
    }
    if preview_url:
        payload["pull_request"]["deployment_url"] = preview_url
    return payload


def make_store(agent_bus: FakeAgentBusClient, github: FakeGitHubClient, hermes: FakeHermesClient) -> AgentBusRuntimeValidationStore:
    return AgentBusRuntimeValidationStore(
        hermes_client_factory=lambda: hermes,
        agent_bus_client_factory=lambda _settings: agent_bus,
        github_client_factory=lambda _settings: github,
    )


def test_frontend_pr_vercel_ready_dispatches_hermes_and_records_agent_bus_sequence() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    agent_bus = FakeAgentBusClient()
    github = FakeGitHubClient()
    hermes = FakeHermesClient()
    store = make_store(agent_bus, github, hermes)
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=github))

    result = run(store.trigger(request, settings()))

    assert result.status == "completed"
    assert hermes.payloads[0]["payload"]["validation_type"] == "playwright"
    assert hermes.payloads[0]["payload"]["repo"] == "marcus937/jarvis-mission-control"
    assert hermes.payloads[0]["payload"]["work_item_id"] == "work-item-1"
    assert [state["state"] for state in agent_bus.states] == [
        RuntimeValidationState.REQUESTED.value,
        RuntimeValidationState.RUNNING.value,
        RuntimeValidationState.PLAYWRIGHT_EXECUTED.value,
        RuntimeValidationState.PASSED.value,
    ]


def test_frontend_pr_vercel_failed_records_blocked_without_hermes_dispatch() -> None:
    parsed = parse_github_event("pull_request", pr_payload(preview_url=None))
    github = FakeGitHubClient(statuses=[{"context": "Vercel", "state": "failure", "target_url": "https://vercel.com/deployments/1"}])
    agent_bus = FakeAgentBusClient()
    hermes = FakeHermesClient()
    store = make_store(agent_bus, github, hermes)
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=github))

    result = run(store.trigger(request, settings()))

    assert result.status == "blocked"
    assert hermes.payloads == []
    assert agent_bus.states[-1]["state"] == RuntimeValidationState.BLOCKED.value
    assert agent_bus.states[-1]["result"] == "blocked"


def test_hermes_passed_creates_github_comment_label_and_commit_status() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    agent_bus = FakeAgentBusClient()
    github = FakeGitHubClient()
    hermes = FakeHermesClient({"status": "PASSED", "jobId": "job-ok"})
    store = make_store(agent_bus, github, hermes)
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=github))

    result = run(store.trigger(request, settings()))

    assert result.bb2.review_status == "approved"
    assert "## Hermes Runtime Validation" in github.comments[0][2]
    assert github.labels == [("marcus937/jarvis-mission-control", 134, "agent-verified")]
    assert github.commit_statuses[0][2]["context"] == "Hermes Playwright Validation"
    assert github.commit_statuses[0][2]["state"] == "success"


def test_hermes_failed_prevents_ready_for_review_by_agent_bus_result() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    agent_bus = FakeAgentBusClient()
    github = FakeGitHubClient()
    hermes = FakeHermesClient({"status": "FAILED", "jobId": "job-fail"})
    store = make_store(agent_bus, github, hermes)
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=github))

    result = run(store.trigger(request, settings()))

    assert result.bb2.review_status == "needs_changes"
    assert agent_bus.states[-1]["state"] == RuntimeValidationState.FAILED.value
    assert agent_bus.states[-1]["result"] == "failed"
    assert github.labels == [("marcus937/jarvis-mission-control", 134, "agent-revisions")]


def test_backend_only_repository_skips_hermes() -> None:
    parsed = parse_github_event("pull_request", pr_payload(repo="marcus937/jarvis-agent-bus-mcp", preview_url=None))

    assert runtime_validation_required_for_parsed(parsed, settings(), has_review_context=False) is False
    assert frontend_validation_profile_for_repo(parsed.repository).requires_runtime_validation is False


def test_documentation_only_work_skips_hermes() -> None:
    parsed = parse_github_event("pull_request", pr_payload(labels=["documentation-only"]))

    assert runtime_validation_required_for_parsed(parsed, settings(), has_review_context=False) is False


def test_vercel_timeout_records_blocked() -> None:
    parsed = parse_github_event("pull_request", pr_payload(preview_url=None))
    github = FakeGitHubClient()
    readiness, target_url, source, reason = run(resolve_vercel_readiness(parsed, github))

    assert readiness == VercelReadiness.TIMEOUT
    assert target_url is None
    assert source == "vercel_timeout"
    assert "Timed out" in reason


def test_runtime_validation_visible_through_result_review_dispatch() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    agent_bus = FakeAgentBusClient()
    github = FakeGitHubClient()
    hermes = FakeHermesClient()
    store = make_store(agent_bus, github, hermes)
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=github))

    result = run(store.trigger(request, settings()))

    assert result.workflow_id.startswith("wf20-")
    assert result.review_dispatch["agent_bus_runtime_validation"]["current_state"] == "HERMES_VALIDATION_PASSED"
    assert result.work_item_id == "work-item-1"
