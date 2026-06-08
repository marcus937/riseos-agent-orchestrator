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


def _post_agent_integration_push(client: TestClient, secret: str = "test-secret") -> None:
    payload = {
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "agent"},
        "ref": "refs/heads/agent-integration",
        "after": "abc123",
    }
    body = json.dumps(payload).encode("utf-8")
    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "push", body))
    assert response.status_code == 200


def test_workflow_endpoints_return_canonical_record_and_timeline() -> None:
    client = client_with_secret()
    _post_agent_integration_push(client)

    collection = client.get("/api/v1/workflows")

    assert collection.status_code == 200
    workflows = collection.json()["workflows"]
    assert len(workflows) == 1
    workflow = workflows[0]
    assert workflow["workflow_id"].startswith("wf-")
    assert workflow["repo_full_name"] == "riseos/example"
    assert workflow["current_state"] == "CIRCUIT_WORKING"
    assert workflow["last_actor"] == "circuit-forge"
    assert workflow["timeline"][0]["event_type"] == "workflow.lifecycle.changed"
    assert workflow["timeline"][0]["new_state"] == "CIRCUIT_WORKING"
    assert workflow["route_history"] == ["circuit-forge: CIRCUIT_WORKING"]

    detail = client.get(f"/api/v1/workflows/{workflow['workflow_id']}")
    timeline = client.get(f"/api/v1/workflows/{workflow['workflow_id']}/timeline")

    assert detail.status_code == 200
    assert detail.json()["workflow_id"] == workflow["workflow_id"]
    assert timeline.status_code == 200
    assert timeline.json()["events"][0]["new_state"] == "CIRCUIT_WORKING"


def test_workflow_endpoints_use_debug_read_access_policy() -> None:
    client = client_with_secret(require_debug_read_token=True)

    assert client.get("/api/v1/workflows").status_code == 401
    response = client.get("/api/v1/workflows", headers={"X-Orchestrator-Admin-Token": "admin-token"})

    assert response.status_code == 200
    assert response.json()["workflows"] == []
