import json

from fastapi.testclient import TestClient

from app.config import get_settings
from app.event_store import event_store
from app.main import app
from app.review_queue import review_queue
from app.security import build_signature


def client_with_secret(
    secret: str = "test-secret",
    admin_token: str = "admin-token",
    require_debug_read_token: bool = False,
) -> TestClient:
    get_settings.cache_clear()
    event_store.reset()
    review_queue.reset()
    app.dependency_overrides[get_settings] = lambda: get_settings().__class__(
        github_webhook_secret=secret,
        orchestrator_admin_token=admin_token,
        require_admin_token_for_debug_reads=require_debug_read_token,
        hermes_m2_token="hermes-m2-secret",
        hermes_dgx_token="hermes-dgx-secret",
    )
    return TestClient(app)


def signed_headers(secret: str, event: str, payload: bytes) -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": build_signature(secret, payload),
        "Content-Type": "application/json",
    }


def test_orchestrator_snapshot_aggregates_existing_telemetry_sources() -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    payload = {
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "agent"},
        "ref": "refs/heads/agent-integration",
        "after": "abc123",
    }
    body = json.dumps(payload).encode("utf-8")
    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "push", body))
    assert response.status_code == 200

    snapshot = client.get("/api/v1/orchestrator/snapshot")

    assert snapshot.status_code == 200
    data = snapshot.json()
    assert data["schema_version"] == "orchestrator.snapshot.v1"
    assert data["generated_at"]
    assert set(data) >= {"workforce", "queue", "health", "runtime", "recent_failures"}
    assert "overview" not in data
    assert "agents" not in data
    workforce = data["workforce"]
    assert set(workforce) == {"overview", "agents", "issues", "prs", "events"}
    assert workforce["overview"]["status"] == "ok"
    assert workforce["overview"]["work_branch"] == "agent-integration"
    assert workforce["overview"]["base_branch"] == "main"
    assert workforce["overview"]["review_queue_count"] == 1
    assert data["queue"]["counters"]["pending_review_count"] == 1
    assert data["health"]["accepted_count"] == 1
    assert workforce["agents"][0]["item_id"]
    assert workforce["agents"][0]["repo_full_name"] == "riseos/example"
    assert workforce["agents"][0]["workflow_state"] == "CIRCUIT_IN_PROGRESS"
    assert workforce["agents"][0]["current_owner"] == "Circuit"
    assert workforce["agents"][0]["workflow_duration_seconds"] >= 0
    assert workforce["agents"][0]["workflow_state_history"][0]["state"] == "CIRCUIT_IN_PROGRESS"
    assert workforce["agents"][0]["workflow_events"][0]["source"] == "review_work_item"
    assert workforce["events"][0]["repo_full_name"] == "riseos/example"
    assert workforce["events"][0]["commit_sha"] == "abc123"
    assert workforce["events"][0]["workflow_state"] == "CIRCUIT_IN_PROGRESS"
    assert workforce["events"][0]["workflow_state_history"][0]["source"] == "github_webhook"
    assert workforce["issues"] == []
    assert workforce["prs"] == []
    assert data["runtime"]["auto_processing_enabled"] is False
    assert data["runtime"]["hermes_dispatch"]["m2_dispatch_enabled"] is False


def test_orchestrator_snapshot_uses_debug_read_access_policy() -> None:
    client = client_with_secret(require_debug_read_token=True)

    assert client.get("/api/v1/orchestrator/snapshot").status_code == 401
    response = client.get(
        "/api/v1/orchestrator/snapshot",
        headers={"X-Orchestrator-Admin-Token": "admin-token"},
    )

    assert response.status_code == 200
    assert response.json()["schema_version"] == "orchestrator.snapshot.v1"


def test_orchestrator_snapshot_runtime_status_does_not_expose_secret_values() -> None:
    client = client_with_secret()

    response = client.get("/api/v1/orchestrator/snapshot")

    assert response.status_code == 200
    body = response.text
    assert "hermes-m2-secret" not in body
    assert "hermes-dgx-secret" not in body
    data = response.json()
    assert set(data["runtime"]["hermes_dispatch"]) == {
        "default_target_configured",
        "m2_dispatch_enabled",
        "m2_configured",
        "dgx_dispatch_enabled",
        "dgx_configured",
    }
