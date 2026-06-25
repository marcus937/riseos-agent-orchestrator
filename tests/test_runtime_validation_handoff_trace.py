import json
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationHermesSummary,
    RuntimeValidationResult,
)
from app.config import get_settings
from app.main import app, runtime_validation_store as canonical_runtime_validation_store
from app.wf20_runtime_validation import AgentBusRuntimeValidationStore


class FakeHermesHTTPResponse:
    def __init__(self, status_code: int, payload: dict[str, Any], *, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {"content-type": "application/json"}
        self.text = json.dumps(payload)
        self.content = self.text.encode("utf-8")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeHermesAsyncClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.posts: list[dict[str, Any]] = []
        self.gets: list[str] = []
        self.closed = False

    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> FakeHermesHTTPResponse:
        self.posts.append({"url": url, "headers": headers, "json": json})
        return FakeHermesHTTPResponse(202, {"status": "PASSED", "jobId": "job-runtime-trace-1"})

    async def get(self, url: str, *, headers: dict[str, str]) -> FakeHermesHTTPResponse:
        self.gets.append(url)
        if url.endswith("/manifest"):
            return FakeHermesHTTPResponse(
                200,
                {
                    "pageTitle": "Jarvis Mission Control",
                    "finalUrl": "https://jarvis-mission-control-gules.vercel.app",
                    "httpStatus": 200,
                    "artifacts": [{"fileName": "summary.json", "contentType": "application/json", "size": 123, "sha256": "abc123"}],
                },
            )
        return FakeHermesHTTPResponse(200, {"ok": True}, headers={"content-type": "application/zip"})

    async def aclose(self) -> None:
        self.closed = True


class NoOutcomeStore:
    async def trigger(self, request: Any, settings: Any) -> RuntimeValidationResult:
        return RuntimeValidationResult(
            validation_id="rv-no-outcome",
            status="pending",
            repo=request.repo,
            pr_number=request.pr_number,
            branch=request.branch,
            base_branch=request.base_branch,
            workflow_id=request.workflow_id,
            work_item_id=request.work_item_id,
            validation_type=request.validation_type,
            requested_by=request.requested_by,
            created_at=datetime.now(UTC),
            correlation_id="rv-no-outcome",
            hermes=RuntimeValidationHermesSummary(status="SKIPPED"),
            evidence=RuntimeValidationEvidenceSummary(),
            bb2=RuntimeValidationBB2Packet(),
        )

    def get(self, validation_id: str) -> None:
        return None


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("ORCHESTRATOR_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("HERMES_M2_BASE_URL", "https://hermes.example.test")
    monkeypatch.setenv("HERMES_M2_TOKEN", "hermes-secret")
    monkeypatch.setenv("HERMES_M2_ENABLE_DISPATCH", "true")
    monkeypatch.setenv("HERMES_DEFAULT_TARGET", "https://jarvis-mission-control-gules.vercel.app")
    monkeypatch.delenv("ENABLE_AGENT_BUS_DISPATCH", raising=False)
    monkeypatch.setattr(
        "app.circuit_runtime_validation.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    get_settings.cache_clear()
    return TestClient(app, raise_server_exceptions=False)


def _request() -> dict[str, Any]:
    return {
        "repo": "marcus937/jarvis-mission-control",
        "issue_number": 43,
        "pr_number": 38,
        "branch": "agent-integration",
        "target_url": "https://jarvis-mission-control-gules.vercel.app",
        "requested_by": "circuit",
        "workflow_id": "wf20-trace-1",
        "work_item_id": "wi-trace-1",
        "evidence_id": "evidence-trace-1",
        "review_dispatch": {"commit_sha": "abc123def456"},
    }


def _log_events(caplog) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for record in caplog.records:
        try:
            events.append(json.loads(record.getMessage()))
        except json.JSONDecodeError:
            continue
    return events


def test_runtime_validation_handoff_uses_canonical_store_and_calls_hermes_http(monkeypatch, caplog) -> None:
    fake_http = FakeHermesAsyncClient()
    monkeypatch.setattr("app.hermes_dispatch_impl.httpx.AsyncClient", lambda *args, **kwargs: fake_http)
    app.state.runtime_validation_store = canonical_runtime_validation_store
    canonical_runtime_validation_store._items.clear()
    client = _client(monkeypatch)
    caplog.set_level("INFO", logger="riseos_agent_orchestrator")

    response = client.post(
        "/api/v1/runtime-validations",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json=_request(),
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["hermes"]["job_id"] == "job-runtime-trace-1"
    assert isinstance(app.state.runtime_validation_store, AgentBusRuntimeValidationStore)
    assert fake_http.posts
    assert fake_http.posts[0]["url"] == "https://hermes.example.test/api/v1/jobs"
    assert fake_http.posts[0]["json"]["payload"]["targetUrl"] == "https://jarvis-mission-control-gules.vercel.app"
    assert fake_http.posts[0]["json"]["payload"]["commitSha"] == "abc123def456"

    events = _log_events(caplog)
    event_names = {event.get("event") for event in events}
    assert "runtime_validation_store_selected" in event_names
    assert "agent_bus_runtime_validation_store_entered" in event_names
    assert "runtime_validation_trigger_boundary_entered" in event_names
    assert "hermes_dispatch_started" in event_names
    assert "hermes_dispatch_completed" in event_names
    assert "hermes_evidence_collected" in event_names
    assert "runtime_validation_created_response_contract_satisfied" in event_names


def test_runtime_validation_created_response_requires_dispatch_or_explicit_skip_reason(monkeypatch, caplog) -> None:
    app.state.runtime_validation_store = NoOutcomeStore()
    client = _client(monkeypatch)
    caplog.set_level("INFO", logger="riseos_agent_orchestrator")

    response = client.post(
        "/api/v1/runtime-validations",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json=_request(),
    )

    assert response.status_code == 500
    assert response.json()["detail"]["error"] == "runtime_validation_created_without_handoff_outcome"
    events = _log_events(caplog)
    assert any(event.get("event") == "runtime_validation_created_response_contract_failed" for event in events)
