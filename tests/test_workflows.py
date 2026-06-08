import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.circuit_runtime_validation_routes import register_circuit_runtime_validation_routes
from app.config import get_settings
from app.event_store import event_store
from app.main import app
from app.review_queue import review_queue
from app.security import build_signature
from app.workflow_routes import register_workflow_routes


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


def _post_pull_request_event(client: TestClient, action: str, *, merged: bool, secret: str = "test-secret") -> None:
    payload = {
        "action": action,
        "number": 17,
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "human"},
        "pull_request": {
            "number": 17,
            "merged": merged,
            "head": {
                "sha": "def456",
                "ref": "agent-integration",
                "repo": {"full_name": "riseos/example"},
            },
            "base": {
                "ref": "main",
                "repo": {"full_name": "riseos/example"},
            },
            "labels": [],
        },
    }
    body = json.dumps(payload).encode("utf-8")
    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "pull_request", body))
    assert response.status_code == 200


def test_workflow_routes_are_registered_from_application_composition() -> None:
    runtime_only_app = FastAPI()
    register_circuit_runtime_validation_routes(runtime_only_app)
    assert any(route.path.startswith("/api/v1/runtime-validations") for route in runtime_only_app.routes)
    assert not any(route.path.startswith("/api/v1/workflows") for route in runtime_only_app.routes)

    composed_app = FastAPI()
    register_workflow_routes(composed_app)
    register_circuit_runtime_validation_routes(composed_app)
    assert any(route.path == "/api/v1/workflows" for route in composed_app.routes)
    assert any(route.path.startswith("/api/v1/runtime-validations") for route in composed_app.routes)


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
    assert workflow["last_actor"] == "Circuit"
    assert workflow["timeline"][0]["event_type"] == "workflow.lifecycle.changed"
    assert workflow["timeline"][0]["state"] == "CIRCUIT_IN_PROGRESS"
    assert workflow["timeline"][0]["canonical_state"] == "CIRCUIT_WORKING"
    assert workflow["timeline"][0]["new_state"] == "CIRCUIT_WORKING"
    assert workflow["route_history"] == ["Circuit: CIRCUIT_WORKING"]

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


def test_pull_request_closed_is_not_automatically_merged() -> None:
    client = client_with_secret()
    _post_pull_request_event(client, "closed", merged=False)

    collection = client.get("/api/v1/workflows")

    assert collection.status_code == 200
    workflows = collection.json()["workflows"]
    assert workflows[0]["current_state"] == "CLOSED_UNMERGED"
    assert workflows[0]["timeline"][0]["state"] == "CLOSED_UNMERGED"


def test_pull_request_closed_merged_is_explicitly_merged() -> None:
    client = client_with_secret()
    _post_pull_request_event(client, "closed", merged=True)

    collection = client.get("/api/v1/workflows")

    assert collection.status_code == 200
    workflows = collection.json()["workflows"]
    assert workflows[0]["current_state"] == "MERGED"
    assert workflows[0]["timeline"][0]["state"] == "MERGED"
