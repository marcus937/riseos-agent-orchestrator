import asyncio
import json
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent_tasks import AgentTaskCreateRequest, InMemoryAgentTaskStore, create_agent_task, mark_agent_task_assigned
from app.circuit_runtime_validation import runtime_validation_store as base_runtime_validation_store
from app.circuit_runtime_validation_routes import register_circuit_runtime_validation_routes
from app.config import get_settings
from app.hermes_dispatch import HermesEvidenceArtifact, HermesEvidenceSnapshot
from app.main import app, runtime_validation_store as canonical_runtime_validation_store
from app.review_queue import review_queue
from app.reviewer.decision import ReviewDecisionType
from app.task_dispatch import dispatch_workflow_chain_continuation
from app.wf20_runtime_validation import AgentBusRuntimeValidationStore
from app.workflow_continuation import SQLiteWorkflowContinuationStore


class FakeRuntimeHermesClient:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    async def post_runtime_validation(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.jobs.append((base_url, token, payload))
        return {"status": "PASSED", "jobId": "job-circuit-123"}

    async def collect_evidence(
        self,
        base_url: str,
        token: str,
        job_id: str,
        settings: Any,
    ) -> HermesEvidenceSnapshot:
        snapshot = HermesEvidenceSnapshot(
            job_id=job_id,
            manifest_fetched=True,
            bundle_fetched=True,
            page_title="Jarvis Mission Control",
            final_url="https://jarvis-mission-control-gules.vercel.app",
            http_status=200,
            screenshot_present=True,
            console_warning_count=0,
            console_error_count=0,
            network_failure_count=0,
            network_non_2xx_count=0,
            artifacts=[
                HermesEvidenceArtifact(
                    file_name="summary.json",
                    content_type="application/json",
                    size=123,
                    sha256="abc123",
                    retrieval_note="GET /api/v1/evidence/job-circuit-123/files/summary.json",
                )
            ],
        )
        object.__setattr__(snapshot, "viewport", {"width": 1280, "height": 720})
        object.__setattr__(snapshot, "user_agent", "Playwright Chromium")
        object.__setattr__(snapshot, "load_duration", 321)
        object.__setattr__(snapshot, "console_info_count", 0)
        object.__setattr__(snapshot, "console_log_count", 0)
        object.__setattr__(snapshot, "network_request_count", 5)
        object.__setattr__(snapshot, "network_response_count", 5)
        return snapshot

    async def aclose(self) -> None:
        self.closed = True


class FakeContinuationAgentBusClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"work_item_id": "wi-wf22"}


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("ORCHESTRATOR_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("HERMES_M2_BASE_URL", "https://hermes.example.test")
    monkeypatch.setenv("HERMES_M2_TOKEN", "hermes-secret")
    monkeypatch.setenv("HERMES_M2_ENABLE_DISPATCH", "true")
    monkeypatch.setenv("HERMES_DEFAULT_TARGET", "https://jarvis-mission-control-gules.vercel.app")
    monkeypatch.setenv("ENABLE_RUNTIME_VALIDATION_REVIEW_BRIDGE", "true")
    monkeypatch.setenv("ENABLE_AUTO_REVIEW_PROCESSING", "true")
    monkeypatch.delenv("ENABLE_AGENT_BUS_DISPATCH", raising=False)
    monkeypatch.setattr(
        "app.circuit_runtime_validation.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    app.state.runtime_validation_store = canonical_runtime_validation_store
    canonical_runtime_validation_store._items.clear()
    review_queue.reset()
    get_settings.cache_clear()
    return TestClient(app)


def _request(target_url: str = "https://jarvis-mission-control-gules.vercel.app") -> dict[str, Any]:
    return {
        "repo": "marcus937/jarvis-mission-control",
        "issue_number": 43,
        "pr_number": 38,
        "branch": "agent-integration",
        "target_url": target_url,
        "requested_by": "circuit",
    }


def _log_events(caplog) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for record in caplog.records:
        try:
            events.append(json.loads(record.getMessage()))
        except json.JSONDecodeError:
            continue
    return events


def test_runtime_validation_routes_are_registered_explicitly_and_idempotently() -> None:
    route_count = len(app.routes)
    register_circuit_runtime_validation_routes(app)
    register_circuit_runtime_validation_routes(app)
    route_paths = {getattr(route, "path", None) for route in app.routes}

    assert len(app.routes) == route_count
    assert not hasattr(FastAPI, "_circuit_runtime_validation_patch_installed")
    assert "/api/v1/runtime-validations" in route_paths
    assert "/api/v1/runtime-validations/{validation_id}" in route_paths
    assert "/api/v1/runtime-validations/{validation_id}/evidence" in route_paths
    assert "/api/v1/runtime-validations/{validation_id}/bb2-packet" in route_paths


def test_runtime_validation_route_uses_app_state_agent_bus_store_not_base_store(monkeypatch, caplog) -> None:
    client = _client(monkeypatch)
    assert app.state.runtime_validation_store is canonical_runtime_validation_store
    assert isinstance(app.state.runtime_validation_store, AgentBusRuntimeValidationStore)

    async def _base_store_must_not_be_used(*args: object, **kwargs: object) -> object:
        raise AssertionError("/runtime-validations must not use the base module-level RuntimeValidationStore")

    monkeypatch.setattr(base_runtime_validation_store, "trigger", _base_store_must_not_be_used)
    caplog.set_level("INFO", logger="riseos_agent_orchestrator")

    response = client.post(
        "/api/v1/runtime-validations",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json=_request("http://127.0.0.1:3000"),
    )

    assert response.status_code == 201
    store_logs = [event for event in _log_events(caplog) if event.get("event") == "runtime_validation_store_selected"]
    assert store_logs
    assert store_logs[-1]["dispatch_path"] == "runtime_validation_route"
    assert store_logs[-1]["store_class"] == "AgentBusRuntimeValidationStore"
    assert store_logs[-1]["store_module"] == "app.wf20_runtime_validation"


def test_runtime_validation_requires_admin_token(monkeypatch) -> None:
    client = _client(monkeypatch)

    response = client.post("/api/v1/runtime-validations", json=_request())

    assert response.status_code == 401


def test_runtime_validation_read_endpoints_require_admin_token(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.post(
        "/api/v1/runtime-validations",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json=_request("http://127.0.0.1:3000"),
    )
    validation_id = response.json()["validation_id"]

    for path in [
        f"/api/v1/runtime-validations/{validation_id}",
        f"/api/v1/runtime-validations/{validation_id}/evidence",
        f"/api/v1/runtime-validations/{validation_id}/bb2-packet",
    ]:
        assert client.get(path).status_code == 401


def test_runtime_validation_blocks_unsafe_target_and_can_be_retrieved(monkeypatch) -> None:
    client = _client(monkeypatch)

    response = client.post(
        "/api/v1/runtime-validations",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json=_request("http://127.0.0.1:3000"),
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["bb2"]["review_status"] == "blocked"
    assert "trusted Vercel" in payload["error"]

    validation_id = payload["validation_id"]
    result = client.get(
        f"/api/v1/runtime-validations/{validation_id}",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
    )
    evidence = client.get(
        f"/api/v1/runtime-validations/{validation_id}/evidence",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
    )
    bb2_packet = client.get(
        f"/api/v1/runtime-validations/{validation_id}/bb2-packet",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
    )

    assert result.status_code == 200
    assert evidence.status_code == 200
    assert bb2_packet.status_code == 200
    assert bb2_packet.json()["review_status"] == "blocked"


def test_runtime_validation_blocks_credential_bearing_url(monkeypatch) -> None:
    client = _client(monkeypatch)

    response = client.post(
        "/api/v1/runtime-validations",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json=_request("https://user:pass@jarvis-mission-control-gules.vercel.app"),
    )

    assert response.status_code == 201
    assert response.json()["status"] == "blocked"
    assert "credentials" in response.json()["error"]


def test_runtime_validation_blocks_private_dns_resolution(monkeypatch) -> None:
    client = _client(monkeypatch)
    monkeypatch.setattr(
        "app.circuit_runtime_validation.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("10.0.0.5", 443))],
    )

    response = client.post(
        "/api/v1/runtime-validations",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json=_request(),
    )

    assert response.status_code == 201
    assert response.json()["status"] == "blocked"
    assert "private" in response.json()["error"]


def test_frontend_runtime_validation_route_reaches_hermes_dispatch_with_verified_target(monkeypatch) -> None:
    client = _client(monkeypatch)
    fake = FakeRuntimeHermesClient()
    monkeypatch.setattr(canonical_runtime_validation_store, "_hermes_client_factory", lambda: fake)

    response = client.post(
        "/api/v1/runtime-validations",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json=_request(),
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["hermes"]["job_id"] == "job-circuit-123"
    assert payload["hermes"]["manifest_fetched"] is True
    assert payload["hermes"]["bundle_fetched"] is True
    assert payload["evidence"]["page_title"] == "Jarvis Mission Control"
    assert payload["evidence"]["http_status"] == 200
    assert payload["evidence"]["artifacts"][0]["sha256"] == "abc123"
    assert payload["bb2"]["packet_created"] is True
    assert payload["bb2"]["review_context"]["field_propagation_matrix"]["page_title"] is True
    assert fake.jobs
    assert fake.jobs[0][0] == "https://hermes.example.test"
    assert fake.jobs[0][1] == "hermes-secret"
    assert fake.jobs[0][2]["payload"]["repo"] == "marcus937/jarvis-mission-control"
    assert fake.jobs[0][2]["payload"]["targetUrl"] == "https://jarvis-mission-control-gules.vercel.app"
    assert fake.closed is True


def test_runtime_validation_recovers_workflow_step_from_agent_task_metadata_and_dispatches_next_step(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch)
    fake = FakeRuntimeHermesClient()
    monkeypatch.setattr(canonical_runtime_validation_store, "_hermes_client_factory", lambda: fake)
    monkeypatch.setattr(app.state, "agent_task_store", _agent_task_store_with_wf21_metadata(), raising=False)

    async def fake_review_processor(item: Any, settings: Any) -> Any:
        return None

    monkeypatch.setattr(app.state, "review_processor", fake_review_processor, raising=False)

    payload = _request("https://jarvis-mission-control-gules.vercel.app") | {
        "branch": "codex-m2/wf21-chain",
        "base_branch": "agent-integration",
        "work_item_id": "wi-wf21",
        "workflow_id": "wf-chain-123",
        "review_dispatch": {
            "title": "BB2 review for WF21",
            "repository": "marcus937/jarvis-mission-control",
            "pr_number": 38,
            "branch": "codex-m2/wf21-chain",
        },
    }

    response = client.post(
        "/api/v1/runtime-validations",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json=payload,
    )

    assert response.status_code == 201
    item = review_queue.list_items()[0]
    context = item.runtime_validation_context
    review_dispatch = context["review_dispatch"]
    assert review_dispatch["workflow_step"] == "WF21"
    assert review_dispatch["current_workflow_step"] == "WF21"
    assert review_dispatch["workflow_chain_id"] == "wf-chain-123"
    assert review_dispatch["workflow_steps"] == ["WF21", "WF22", "WF23"]
    assert review_dispatch["next_workflow_step"] == "WF22"

    agent_bus = FakeContinuationAgentBusClient()
    continuation_store = SQLiteWorkflowContinuationStore(str(tmp_path / "continuations.db"))
    first = asyncio.run(
        dispatch_workflow_chain_continuation(
            item,
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=continuation_store,
            base_branch="agent-integration",
        )
    )
    second = asyncio.run(
        dispatch_workflow_chain_continuation(
            item,
            ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
            enabled=True,
            agent_bus_client=agent_bus,
            agent_bus_enabled=True,
            continuation_store=continuation_store,
            base_branch="agent-integration",
        )
    )

    assert first is not None and first.success is True
    assert second is not None and second.success is True
    assert len(agent_bus.payloads) == 1
    next_payload = agent_bus.payloads[0]
    assert next_payload["pr_number"] == 38
    assert next_payload["metadata"]["workflow_step"] == "WF22"
    assert next_payload["metadata"]["previous_workflow_step"] == "WF21"
    assert next_payload["metadata"]["branch"] == "codex-m2/wf21-chain"
    continuations = continuation_store.list_workflow_continuations("wf-chain-123")
    assert len(continuations) == 1
    assert continuations[0].current_workflow_step == "WF21"
    assert continuations[0].current_workflow_step != "UNKNOWN"
    assert continuations[0].next_workflow_step == "WF22"


def test_runtime_validation_non_workflow_context_is_unchanged(monkeypatch) -> None:
    client = _client(monkeypatch)
    fake = FakeRuntimeHermesClient()
    monkeypatch.setattr(canonical_runtime_validation_store, "_hermes_client_factory", lambda: fake)

    response = client.post(
        "/api/v1/runtime-validations",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json=_request(),
    )

    assert response.status_code == 201
    item = review_queue.list_items()[0]
    review_dispatch = item.runtime_validation_context["review_dispatch"]
    assert "workflow_step" not in review_dispatch
    assert "workflow_chain_id" not in review_dispatch


def test_runtime_validation_completion_schedules_bb2_review_processing(monkeypatch) -> None:
    client = _client(monkeypatch)
    fake = FakeRuntimeHermesClient()
    scheduled: list[dict[str, Any]] = []
    monkeypatch.setattr(canonical_runtime_validation_store, "_hermes_client_factory", lambda: fake)

    async def fake_process_queued_review_item(item_id: str, settings: Any, storage: Any, processor: Any) -> None:
        scheduled.append({"item_id": item_id, "storage": storage, "processor": processor})

    async def fake_review_processor(item: Any, settings: Any) -> Any:
        return None

    monkeypatch.setattr(
        "app.circuit_runtime_validation_routes.process_queued_review_item",
        fake_process_queued_review_item,
    )
    monkeypatch.setattr(app.state, "review_processor", fake_review_processor, raising=False)

    response = client.post(
        "/api/v1/runtime-validations",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json=_request(),
    )

    assert response.status_code == 201
    assert scheduled
    assert scheduled[0]["item_id"]
    assert scheduled[0]["processor"] is fake_review_processor


def test_runtime_validation_missing_id_returns_404(monkeypatch) -> None:
    client = _client(monkeypatch)

    response = client.get(
        "/api/v1/runtime-validations/missing-id",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
    )

    assert response.status_code == 404


def _agent_task_store_with_wf21_metadata() -> InMemoryAgentTaskStore:
    store = InMemoryAgentTaskStore()
    task = create_agent_task(
        AgentTaskCreateRequest(
            repo_full_name="marcus937/jarvis-mission-control",
            title="WF21 implementation",
            objective="Implement WF21.",
            target_agent="codex-m2",
            correlation_id="wf-chain-123",
        )
    )
    task.branch = "codex-m2/wf21-chain"
    task.commit_sha = "abc123"
    task.execution_evidence = {
        "_workflow_chain": {
            "workflow_chain_id": "wf-chain-123",
            "workflow_family": "WF21-WF23",
            "workflow_steps": ["WF21", "WF22", "WF23"],
            "workflow_sequence": ["WF21", "WF22", "WF23"],
            "current_workflow_step": "WF21",
            "next_workflow_step": "WF22",
            "final_workflow_step": "WF23",
            "continuation_mode": "same_pr_branch",
            "merge_gate": "final_step_only",
            "repository": "marcus937/jarvis-mission-control",
            "base_branch": "agent-integration",
        }
    }
    mark_agent_task_assigned(task, work_item_id="wi-wf21")
    store.save_agent_task(task)
    return store
