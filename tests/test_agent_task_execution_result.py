from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent_task_routes import router
from app.agent_tasks import InMemoryAgentTaskStore, create_agent_task, AgentTaskCreateRequest
from app.config import Settings, get_settings


def _client() -> tuple[TestClient, InMemoryAgentTaskStore, str]:
    app = FastAPI()
    app.include_router(router)
    store = InMemoryAgentTaskStore()
    task = create_agent_task(
        AgentTaskCreateRequest(
            repo_full_name="marcus937/jarvis-codex-worker",
            title="Validate callback",
            objective="Validate execution-result callback.",
            target_agent="codex-m2",
        )
    )
    store.save_agent_task(task)
    app.state.agent_task_store = store
    app.dependency_overrides[get_settings] = lambda: Settings(orchestrator_admin_token="admin-secret")
    return TestClient(app), store, task.task_id


def test_agent_task_execution_result_accepts_worker_payload() -> None:
    client, store, task_id = _client()

    response = client.post(
        f"/api/v1/agent-tasks/{task_id}/execution-result",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json={
            "agent_id": "codex-m2",
            "status": "completed",
            "commit_sha": "abc123",
            "branch": "codex-m2/validate-callback",
            "changed_files": ["docs/example.md"],
            "evidence": {"summary": "Implementation complete."},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["commit_sha"] == "abc123"
    assert payload["branch"] == "codex-m2/validate-callback"
    assert payload["changed_files"] == ["docs/example.md"]
    assert payload["execution_evidence"] == {"summary": "Implementation complete."}

    saved = store.get_agent_task(task_id)
    assert saved is not None
    assert saved.status == "completed"
    assert saved.lifecycle_events[-1].actor == "codex-m2"


def test_agent_task_execution_result_rejects_wrong_agent() -> None:
    client, _store, task_id = _client()

    response = client.post(
        f"/api/v1/agent-tasks/{task_id}/execution-result",
        headers={"X-Orchestrator-Admin-Token": "admin-secret"},
        json={"agent_id": "other-agent", "status": "completed"},
    )

    assert response.status_code == 409
