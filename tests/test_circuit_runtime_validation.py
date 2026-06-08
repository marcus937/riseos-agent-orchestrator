from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("ORCHESTRATOR_ADMIN_TOKEN", "admin-secret")
    get_settings.cache_clear()
    return TestClient(app)


def test_runtime_validation_requires_admin_token(monkeypatch) -> None:
    client = _client(monkeypatch)

    response = client.post(
        "/api/v1/runtime-validations",
        json={
            "repo": "marcus937/jarvis-mission-control",
            "issue_number": 43,
            "pr_number": 38,
            "target_url": "https://jarvis-mission-control-gules.vercel.app",
            "requested_by": "circuit",
        },
    )

    assert response.status_code == 401


def test_runtime_validation_blocks_unsafe_target_and_can_be_retrieved(monkeypatch) -> None:
    client = _client(monkeypatch)

    response = client.post(
        "/api/v1/runtime-validations",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json={
            "repo": "marcus937/jarvis-mission-control",
            "issue_number": 43,
            "pr_number": 38,
            "target_url": "http://127.0.0.1:3000",
            "requested_by": "circuit",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["bb2"]["review_status"] == "blocked"
    assert "localhost" in payload["error"] or "loopback" in payload["error"]

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


def test_runtime_validation_missing_id_returns_404(monkeypatch) -> None:
    client = _client(monkeypatch)

    response = client.get(
        "/api/v1/runtime-validations/missing-id",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
    )

    assert response.status_code == 404
