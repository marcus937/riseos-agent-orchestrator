from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.agent_tasks import AgentTaskCreateRequest, SQLiteAgentTaskStore, create_agent_task
from app.main import app
from app.repository_discovery import InMemoryRepositoryRegistry, RepositoryRegistryRecord


def _registry(enabled: bool = True) -> InMemoryRepositoryRegistry:
    registry = InMemoryRepositoryRegistry()
    registry.save_repository_registry_record(
        RepositoryRegistryRecord(
            repo_full_name="marcus937/riseos-agent-orchestrator",
            repo_id=1,
            orchestration_enabled=enabled,
            archived=not enabled,
            last_discovered_at=datetime.now(UTC),
        )
    )
    return registry


def _payload() -> dict[str, object]:
    return {
        "repo_full_name": "marcus937/riseos-agent-orchestrator",
        "title": "Create canonical task API",
        "objective": "Add an API entry point for direct coding task submission.",
        "instructions": ["Persist an AgentTask", "Make it visible in workflows"],
        "acceptance_criteria": ["POST returns queued", "GET returns canonical state"],
        "target_agent": "codex-m2",
        "priority": "normal",
        "correlation_id": "external-123",
    }


def test_create_agent_task_persists_queued_task_and_lifecycle_events(tmp_path) -> None:
    store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))

    with TestClient(app) as client:
        app.state.agent_task_store = store
        app.state.repository_registry = _registry()

        created = client.post("/api/v1/agent-tasks", json=_payload())
        listed = client.get("/api/v1/agent-tasks")

    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "queued"
    assert body["target_agent"] == "codex-m2"
    assert body["task_id"].startswith("agtask-")

    assert listed.status_code == 200
    tasks = listed.json()
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == body["task_id"]
    assert tasks[0]["repo_full_name"] == "marcus937/riseos-agent-orchestrator"
    assert tasks[0]["objective"] == "Add an API entry point for direct coding task submission."
    assert tasks[0]["instructions"] == ["Persist an AgentTask", "Make it visible in workflows"]
    assert tasks[0]["acceptance_criteria"] == ["POST returns queued", "GET returns canonical state"]
    assert [event["event"] for event in tasks[0]["lifecycle_events"]] == ["created", "queued"]

    reloaded = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db")).get_agent_task(body["task_id"])
    assert reloaded is not None
    assert reloaded.status == "queued"
    assert reloaded.target_agent == "codex-m2"


def test_get_agent_task_returns_canonical_state(tmp_path) -> None:
    store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))
    task = create_agent_task(AgentTaskCreateRequest(**_payload()))
    store.save_agent_task(task)

    with TestClient(app) as client:
        app.state.agent_task_store = store
        app.state.repository_registry = _registry()

        response = client.get(f"/api/v1/agent-tasks/{task.task_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == task.task_id
    assert body["status"] == "queued"
    assert body["source"] == "direct_api"
    assert body["correlation_id"] == "external-123"


def test_agent_task_submission_rejects_repository_not_enabled(tmp_path) -> None:
    store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))

    with TestClient(app) as client:
        app.state.agent_task_store = store
        app.state.repository_registry = _registry(enabled=False)

        response = client.post("/api/v1/agent-tasks", json=_payload())

    assert response.status_code == 403
    assert response.json()["detail"] == "Repository is not orchestration-enabled."
    assert store.list_agent_tasks() == []


def test_agent_tasks_are_discoverable_through_workflow_api(tmp_path) -> None:
    store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))

    with TestClient(app) as client:
        app.state.agent_task_store = store
        app.state.repository_registry = _registry()

        created = client.post("/api/v1/agent-tasks", json=_payload())
        task_id = created.json()["task_id"]
        workflows = client.get("/api/v1/workflows")
        workflow = client.get(f"/api/v1/workflows/wf-agent-task-{task_id}")
        timeline = client.get(f"/api/v1/workflows/wf-agent-task-{task_id}/timeline")

    assert workflows.status_code == 200
    workflow_records = workflows.json()["workflows"]
    matching = [record for record in workflow_records if record["agent_task_id"] == task_id]
    assert len(matching) == 1
    assert matching[0]["current_state"] == "ASSIGNED"
    assert matching[0]["assigned_agent"] == "codex-m2"
    assert matching[0]["correlation_id"] == "external-123"

    assert workflow.status_code == 200
    assert workflow.json()["agent_task_id"] == task_id

    assert timeline.status_code == 200
    assert [event["metadata"]["agent_task_event"] for event in timeline.json()["events"]] == ["created", "queued"]
