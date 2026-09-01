import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.config import get_settings
from app.event_store import event_store
from app.github_events import GitHubEventType
from app.main import app
from app.orchestrator_snapshot import (
    ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT,
    ORCHESTRATOR_SNAPSHOT_EVENT_LIMIT,
    ORCHESTRATOR_SNAPSHOT_LABEL_LIMIT,
    ORCHESTRATOR_SNAPSHOT_SCHEMA_VERSION,
    ORCHESTRATOR_SNAPSHOT_TEXT_LIMIT,
)
from app.review_queue import ReviewLifecycleStage, ReviewWorkItem, ReviewWorkItemStatus, review_queue
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
    assert data["schema_version"] == ORCHESTRATOR_SNAPSHOT_SCHEMA_VERSION
    assert data["generated_at"]
    assert set(data) >= {"workforce", "workflows", "queue", "health", "runtime", "recent_failures"}
    assert data["workflows"] == {"active": 1, "blocked": 0, "reviewing": 0, "verified": 0}
    assert "overview" not in data
    assert "agents" not in data
    workforce = data["workforce"]
    assert set(workforce) == {"overview", "meta", "agents", "issues", "prs", "events"}
    assert workforce["overview"]["status"] == "ok"
    assert workforce["overview"]["work_branch"] == "agent-integration"
    assert workforce["overview"]["base_branch"] == "main"
    assert workforce["overview"]["review_queue_count"] == 1
    assert workforce["meta"]["agents"] == {
        "returned": 1,
        "total": 1,
        "limit": ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT,
        "truncated": False,
    }
    assert workforce["meta"]["events"] == {
        "returned": 1,
        "total": 1,
        "limit": ORCHESTRATOR_SNAPSHOT_EVENT_LIMIT,
        "truncated": False,
    }
    assert data["queue"]["counters"]["pending_review_count"] == 1
    assert data["health"]["accepted_count"] == 1
    assert workforce["agents"][0]["item_id"]
    assert workforce["agents"][0]["workflow_id"].startswith("wf-")
    assert workforce["agents"][0]["repo_full_name"] == "riseos/example"
    assert workforce["agents"][0]["workflow_state"] == "CIRCUIT_IN_PROGRESS"
    assert workforce["agents"][0]["canonical_workflow_state"] == "CIRCUIT_WORKING"
    assert workforce["agents"][0]["current_owner"] == "Circuit"
    assert workforce["agents"][0]["workflow_duration_seconds"] >= 0
    assert workforce["agents"][0]["workflow_event_count"] == 1
    assert workforce["agents"][0]["workflow_events_truncated"] is True
    assert "workflow_state_history" not in workforce["agents"][0]
    assert "workflow_events" not in workforce["agents"][0]
    assert workforce["events"][0]["repo_full_name"] == "riseos/example"
    assert workforce["events"][0]["commit_sha"] == "abc123"
    assert workforce["events"][0]["workflow_state"] == "CIRCUIT_IN_PROGRESS"
    assert workforce["events"][0]["canonical_workflow_state"] == "CIRCUIT_WORKING"
    assert workforce["events"][0]["workflow_event_count"] == 1
    assert "workflow_state_history" not in workforce["events"][0]
    assert "workflow_events" not in workforce["events"][0]
    assert workforce["issues"] == []
    assert workforce["prs"] == []
    assert data["runtime"]["auto_processing_enabled"] is False
    assert data["runtime"]["hermes_dispatch"]["m2_dispatch_enabled"] is False


def test_orchestrator_snapshot_compacts_large_workforce_payloads() -> None:
    client = client_with_secret()
    huge_historical_payload = "large-runtime-context-" + ("x" * 100_000)
    huge_error = "review failed: " + ("e" * (ORCHESTRATOR_SNAPSHOT_TEXT_LIMIT + 100))
    labels = [f"label-{index}" for index in range(ORCHESTRATOR_SNAPSHOT_LABEL_LIMIT + 5)]
    now = datetime.now(UTC)

    for index in range(ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT + 3):
        item = ReviewWorkItem(
            id=f"item-{index}",
            created_at=now,
            updated_at=now,
            repo_full_name="riseos/example",
            event_type=GitHubEventType.PULL_REQUEST,
            branch=f"feature-{index}",
            base_branch="main",
            commit_sha=f"sha-{index}",
            pr_number=index + 1,
            labels=labels,
            status=ReviewWorkItemStatus.BLOCKED,
            lifecycle_stage=ReviewLifecycleStage.REVIEW_FAILED,
            runtime_validation_context={"historical_payload": huge_historical_payload},
            failure_count=1,
            last_failure_at=now,
            last_error=huge_error,
        )
        review_queue.add_if_absent(item)

    response = client.get("/api/v1/orchestrator/snapshot")

    assert response.status_code == 200
    data = response.json()
    workforce = data["workforce"]
    assert set(data) >= {"workforce", "workflows", "queue", "health", "runtime", "recent_failures"}
    assert len(workforce["prs"]) == ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT
    assert len(workforce["agents"]) == ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT
    assert workforce["meta"]["prs"] == {
        "returned": ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT,
        "total": ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT + 3,
        "limit": ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT,
        "truncated": True,
    }
    assert workforce["meta"]["agents"]["truncated"] is True
    assert data["queue"]["counters"]["review_queue_count"] == ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT + 3
    assert data["workflows"]["blocked"] == ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT + 3

    pr = workforce["prs"][0]
    assert "runtime_validation_context" not in pr
    assert pr["canonical_workflow_state"] == "BLOCKED"
    assert pr["current_owner"] == "BB2"
    assert pr["label_count"] == ORCHESTRATOR_SNAPSHOT_LABEL_LIMIT + 5
    assert len(pr["labels"]) == ORCHESTRATOR_SNAPSHOT_LABEL_LIMIT
    assert pr["labels_truncated"] is True
    assert pr["last_error_truncated"] is True
    assert len(pr["last_error"]) == ORCHESTRATOR_SNAPSHOT_TEXT_LIMIT
    assert "workflow_events" not in pr
    assert "workflow_state_history" not in pr
    assert pr["workflow_event_count"] >= 1
    assert pr["workflow_events_truncated"] is True
    assert workforce["agents"][0]["last_error_truncated"] is True
    assert "workflow_events" not in workforce["agents"][0]
    assert "workflow_state_history" not in workforce["agents"][0]
    assert data["recent_failures"][0]["last_error_truncated"] is True
    assert f"label-{ORCHESTRATOR_SNAPSHOT_LABEL_LIMIT}" not in response.text
    assert "historical_payload" not in response.text
    assert huge_historical_payload not in response.text


def test_orchestrator_snapshot_uses_debug_read_access_policy() -> None:
    client = client_with_secret(require_debug_read_token=True)

    assert client.get("/api/v1/orchestrator/snapshot").status_code == 401
    response = client.get(
        "/api/v1/orchestrator/snapshot",
        headers={"X-Orchestrator-Admin-Token": "admin-token"},
    )

    assert response.status_code == 200
    assert response.json()["schema_version"] == ORCHESTRATOR_SNAPSHOT_SCHEMA_VERSION


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
