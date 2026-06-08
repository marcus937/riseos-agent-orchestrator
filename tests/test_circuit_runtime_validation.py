from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.circuit_runtime_validation import runtime_validation_store
from app.circuit_runtime_validation_routes import register_circuit_runtime_validation_routes
from app.config import get_settings
from app.hermes_dispatch import HermesEvidenceArtifact, HermesEvidenceSnapshot
from app.main import app


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


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("ORCHESTRATOR_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("HERMES_M2_BASE_URL", "https://hermes.example.test")
    monkeypatch.setenv("HERMES_M2_TOKEN", "hermes-secret")
    monkeypatch.setenv("HERMES_M2_ENABLE_DISPATCH", "true")
    monkeypatch.setenv("HERMES_DEFAULT_TARGET", "https://jarvis-mission-control-gules.vercel.app")
    monkeypatch.setattr(
        "app.circuit_runtime_validation.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
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

    assert response.status_code == 200
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

    assert response.status_code == 200
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

    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert "private" in response.json()["error"]


def test_runtime_validation_successful_mocked_hermes_dispatch_hydrates_evidence(monkeypatch) -> None:
    client = _client(monkeypatch)
    fake = FakeRuntimeHermesClient()
    monkeypatch.setattr(runtime_validation_store, "_hermes_client_factory", lambda: fake)

    response = client.post(
        "/api/v1/runtime-validations",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json=_request(),
    )

    assert response.status_code == 200
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
    assert fake.jobs[0][0] == "https://hermes.example.test"
    assert fake.jobs[0][1] == "hermes-secret"
    assert fake.closed is True


def test_runtime_validation_missing_id_returns_404(monkeypatch) -> None:
    client = _client(monkeypatch)

    response = client.get(
        "/api/v1/runtime-validations/missing-id",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
    )

    assert response.status_code == 404
