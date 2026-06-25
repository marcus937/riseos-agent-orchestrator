import asyncio
from typing import Any

from app.config import Settings
from app.github_events import parse_github_event
from app.hermes_dispatch import HermesEvidenceArtifact, HermesEvidenceSnapshot
from app.wf20_deployment_resume import InMemoryWF20DeploymentWaitStore, attach_wf20_deployment_wait_store
from app.wf20_runtime_validation import AgentBusRuntimeValidationStore, RuntimeValidationState
from app.wf20_runtime_validation_safe import runtime_validation_request_from_parsed


class FakeGitHubClient:
    def __init__(self) -> None:
        self.statuses: list[dict[str, Any]] = []
        self.check_runs: list[dict[str, Any]] = []
        self.deployments: list[dict[str, Any]] = []
        self.deployment_statuses: dict[int, list[dict[str, Any]]] = {}
        self.pulls: list[dict[str, Any]] = []
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str]] = []
        self.commit_statuses: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

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


class FakeAgentBusClient:
    def __init__(self) -> None:
        self.created_work_items: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = []
        self.closed = False

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.created_work_items.append(payload)
        return {"work_item_id": f"work-item-{len(self.created_work_items)}", **payload}

    async def record_runtime_validation(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.states.append(payload)
        return {"validation_state_id": f"state-{len(self.states)}", **payload, "metadata": {**payload.get("metadata", {}), "evidence_packet_id": "evidence-1"}}

    async def get_runtime_validation(self, **kwargs: Any) -> dict[str, Any]:
        return {"current_state": RuntimeValidationState.PASSED.value, "history": [{"metadata": {"status": "passed"}}], "query": kwargs}

    async def aclose(self) -> None:
        self.closed = True


class FakeHermesClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.evidence = HermesEvidenceSnapshot(
            job_id="hermes-job-1",
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

    async def post_runtime_validation(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"status": "PASSED", "jobId": "hermes-job-1"}

    async def collect_evidence(self, base_url: str, token: str, job_id: str, settings: Settings) -> HermesEvidenceSnapshot:
        return self.evidence

    async def aclose(self) -> None:
        return None


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def settings() -> Settings:
    return Settings(
        enable_runtime_validation_review_bridge=True,
        enable_agent_bus_dispatch=True,
        agent_bus_base_url="https://agent-bus.test",
        agent_bus_token="agent-token",
        enable_github_writeback=True,
        github_token="github-token",
        hermes_m2_enable_dispatch=True,
        hermes_m2_base_url="https://hermes.test",
        hermes_m2_token="hermes-token",
    )


def pr_payload(*, repo: str = "marcus937/jarvis-mission-control", pr: int = 136, branch: str = "codex-m2/fatal-react-crash", sha: str = "abc123def4567890") -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"full_name": repo},
        "sender": {"login": "marcus"},
        "pull_request": {
            "number": pr,
            "head": {"ref": branch, "sha": sha, "repo": {"full_name": repo}},
            "base": {"ref": "agent-integration", "repo": {"full_name": repo}},
            "labels": [{"name": "frontend"}],
        },
    }


def deployment_status_payload(*, repo: str = "marcus937/jarvis-mission-control", branch: str = "codex-m2/fatal-react-crash", sha: str = "abc123def4567890", state: str = "success", deployment_id: int = 9001, status_id: int = 42, target_url: str = "https://jmc-preview.vercel.app") -> dict[str, Any]:
    return {
        "action": state,
        "repository": {"full_name": repo},
        "sender": {"login": "vercel"},
        "deployment": {"id": deployment_id, "sha": sha, "ref": branch, "environment": "Preview"},
        "deployment_status": {"id": status_id, "state": state, "environment": "Preview", "environment_url": target_url, "target_url": target_url},
    }


def pull_request_record(*, repo: str = "marcus937/jarvis-mission-control", pr: int = 136, branch: str = "codex-m2/fatal-react-crash", sha: str = "abc123def4567890") -> dict[str, Any]:
    return {
        "number": pr,
        "state": "open",
        "head": {"ref": branch, "sha": sha, "repo": {"full_name": repo}},
        "base": {"ref": "agent-integration", "repo": {"full_name": repo}},
        "labels": [{"name": "frontend"}],
    }


def make_runtime(agent_bus: FakeAgentBusClient, github: FakeGitHubClient, hermes: FakeHermesClient, wait_store: InMemoryWF20DeploymentWaitStore) -> AgentBusRuntimeValidationStore:
    store = AgentBusRuntimeValidationStore(
        hermes_client_factory=lambda: hermes,
        agent_bus_client_factory=lambda _settings: agent_bus,
        github_client_factory=lambda _settings: github,
    )
    attach_wf20_deployment_wait_store(store, wait_store)
    return store


def trigger_pr_waiting(runtime: AgentBusRuntimeValidationStore, github: FakeGitHubClient, **payload_kwargs: Any) -> Any:
    parsed = parse_github_event("pull_request", pr_payload(**payload_kwargs))
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=github))
    return run(runtime.trigger(request, settings()))


def trigger_deployment(runtime: AgentBusRuntimeValidationStore, github: FakeGitHubClient, **payload_kwargs: Any) -> Any:
    parsed = parse_github_event("deployment_status", deployment_status_payload(**payload_kwargs))
    github.pulls = [pull_request_record(repo=payload_kwargs.get("repo", "marcus937/jarvis-mission-control"), branch=payload_kwargs.get("branch", "codex-m2/fatal-react-crash"), sha=payload_kwargs.get("sha", "abc123def4567890"))]
    request = run(runtime_validation_request_from_parsed(parsed, settings(), github_client=github))
    return run(runtime.trigger(request, settings()))


def test_deployment_status_arrives_after_workflow_creation_and_resumes_with_preview_url() -> None:
    agent_bus = FakeAgentBusClient()
    github = FakeGitHubClient()
    hermes = FakeHermesClient()
    runtime = make_runtime(agent_bus, github, hermes, InMemoryWF20DeploymentWaitStore())

    waiting = trigger_pr_waiting(runtime, github)
    resumed = trigger_deployment(runtime, github)

    assert waiting.status == "pending"
    assert resumed.status == "completed"
    assert len(hermes.payloads) == 1
    assert hermes.payloads[0]["payload"]["target_url"] == "https://jmc-preview.vercel.app"


def test_deployment_status_arrives_before_commit_status_and_still_resumes() -> None:
    agent_bus = FakeAgentBusClient()
    github = FakeGitHubClient()
    hermes = FakeHermesClient()
    runtime = make_runtime(agent_bus, github, hermes, InMemoryWF20DeploymentWaitStore())

    trigger_pr_waiting(runtime, github)
    github.statuses = []
    github.check_runs = []
    resumed = trigger_deployment(runtime, github)

    assert resumed.status == "completed"
    assert len(hermes.payloads) == 1


def test_multiple_deployment_status_events_and_duplicate_deliveries_launch_once() -> None:
    agent_bus = FakeAgentBusClient()
    github = FakeGitHubClient()
    hermes = FakeHermesClient()
    runtime = make_runtime(agent_bus, github, hermes, InMemoryWF20DeploymentWaitStore())

    trigger_pr_waiting(runtime, github)
    first = trigger_deployment(runtime, github, status_id=42)
    second = trigger_deployment(runtime, github, status_id=43)
    duplicate = trigger_deployment(runtime, github, status_id=42)

    assert first.status == "completed"
    assert second.status == "pending"
    assert duplicate.status == "pending"
    assert len(hermes.payloads) == 1


def test_unrelated_repository_and_unrelated_branch_noop() -> None:
    agent_bus = FakeAgentBusClient()
    github = FakeGitHubClient()
    hermes = FakeHermesClient()
    runtime = make_runtime(agent_bus, github, hermes, InMemoryWF20DeploymentWaitStore())

    trigger_pr_waiting(runtime, github)
    unrelated_repo = trigger_deployment(runtime, github, repo="marcus937/rise-signal", sha="zzz999", branch="codex-m2/fatal-react-crash")
    unrelated_branch = trigger_deployment(runtime, github, sha="other123", branch="codex-m2/other-branch")

    assert unrelated_repo.status == "pending"
    assert unrelated_branch.status == "pending"
    assert hermes.payloads == []


def test_multiple_workflows_waiting_simultaneously_match_independently() -> None:
    agent_bus = FakeAgentBusClient()
    github = FakeGitHubClient()
    hermes = FakeHermesClient()
    runtime = make_runtime(agent_bus, github, hermes, InMemoryWF20DeploymentWaitStore())

    trigger_pr_waiting(runtime, github, pr=136, branch="codex-m2/one", sha="sha-one")
    trigger_pr_waiting(runtime, github, pr=137, branch="codex-m2/two", sha="sha-two")
    trigger_deployment(runtime, github, branch="codex-m2/two", sha="sha-two")
    trigger_deployment(runtime, github, branch="codex-m2/one", sha="sha-one")

    assert len(hermes.payloads) == 2
    assert {payload["payload"]["branch"] for payload in hermes.payloads} == {"codex-m2/one", "codex-m2/two"}


def test_hermes_is_not_started_until_deployment_status_ready() -> None:
    agent_bus = FakeAgentBusClient()
    github = FakeGitHubClient()
    hermes = FakeHermesClient()
    runtime = make_runtime(agent_bus, github, hermes, InMemoryWF20DeploymentWaitStore())

    trigger_pr_waiting(runtime, github)
    pending = trigger_deployment(runtime, github, state="pending")
    ready = trigger_deployment(runtime, github, state="success")

    assert pending.status == "pending"
    assert ready.status == "completed"
    assert len(hermes.payloads) == 1


def test_workflow_resumes_exactly_once_with_verified_preview_url() -> None:
    agent_bus = FakeAgentBusClient()
    github = FakeGitHubClient()
    hermes = FakeHermesClient()
    runtime = make_runtime(agent_bus, github, hermes, InMemoryWF20DeploymentWaitStore())

    trigger_pr_waiting(runtime, github)
    resumed = trigger_deployment(runtime, github, target_url="https://verified-preview.vercel.app")
    trigger_deployment(runtime, github, target_url="https://verified-preview.vercel.app")

    assert resumed.status == "completed"
    assert len(hermes.payloads) == 1
    assert hermes.payloads[0]["payload"]["target_url"] == "https://verified-preview.vercel.app"
