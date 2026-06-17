from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from app.agent_task_dispatch import build_agent_bus_work_item_payload
from app.agent_tasks import AgentTaskCreateRequest, SQLiteAgentTaskStore, create_agent_task
from app.config import get_settings
from app.main import app
from app.repository_discovery import InMemoryRepositoryRegistry, RepositoryRegistryRecord


class FakeAgentBusClient:
    def __init__(self, work_item_id: str = "work-item-123") -> None:
        self.work_item_id = work_item_id
        self.payloads: list[dict[str, Any]] = []

    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"work_item_id": self.work_item_id, "status": "queued"}


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


def _clear_agent_bus_client() -> None:
    if hasattr(app.state, "agent_bus_client"):
        delattr(app.state, "agent_bus_client")


def test_create_agent_task_persists_queued_task_and_lifecycle_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_BUS_DISPATCH", "false")
    get_settings.cache_clear()
    _clear_agent_bus_client()
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
    get_settings.cache_clear()


def test_create_agent_task_dispatches_agent_bus_work_item_when_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_BUS_DISPATCH", "true")
    monkeypatch.setenv("AGENT_BUS_BASE_URL", "https://agent-bus.riseconnect.us")
    get_settings.cache_clear()
    fake_bus = FakeAgentBusClient()
    store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))

    with TestClient(app) as client:
        app.state.agent_task_store = store
        app.state.repository_registry = _registry()
        app.state.agent_bus_client = fake_bus

        created = client.post("/api/v1/agent-tasks", json=_payload())

    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "assigned"
    assert fake_bus.payloads[0]["repository"] == "marcus937/riseos-agent-orchestrator"
    assert fake_bus.payloads[0]["owner_agent"] == "codex-m2"
    assert fake_bus.payloads[0]["metadata"]["task_id"] == body["task_id"]
    assert fake_bus.payloads[0]["metadata"]["workflow_id"] == f"wf-agent-task-{body['task_id']}"
    assert fake_bus.payloads[0]["metadata"]["repo_full_name"] == "marcus937/riseos-agent-orchestrator"
    assert fake_bus.payloads[0]["metadata"]["objective"] == "Add an API entry point for direct coding task submission."
    assert fake_bus.payloads[0]["metadata"]["instructions"] == ["Persist an AgentTask", "Make it visible in workflows"]
    assert fake_bus.payloads[0]["metadata"]["acceptance_criteria"] == ["POST returns queued", "GET returns canonical state"]
    assert fake_bus.payloads[0]["metadata"]["target_agent"] == "codex-m2"

    reloaded = store.get_agent_task(body["task_id"])
    assert reloaded is not None
    assert reloaded.status == "assigned"
    assert reloaded.agent_bus_work_item_id == "work-item-123"
    assert [event.event for event in reloaded.lifecycle_events] == ["created", "queued", "assigned"]
    _clear_agent_bus_client()
    get_settings.cache_clear()


def test_agent_bus_work_item_payload_contains_required_bridge_metadata() -> None:
    task = create_agent_task(AgentTaskCreateRequest(**_payload()))

    payload = build_agent_bus_work_item_payload(task)

    assert payload["title"] == task.title
    assert payload["repository"] == task.repo_full_name
    assert payload["owner_agent"] == "codex-m2"
    assert payload["review_agent"] == "bb2"
    assert payload["metadata"]["task_id"] == task.task_id
    assert payload["metadata"]["workflow_id"] == f"wf-agent-task-{task.task_id}"
    assert payload["metadata"]["repo_full_name"] == task.repo_full_name
    assert payload["metadata"]["objective"] == task.objective
    assert payload["metadata"]["instructions"] == task.instructions
    assert payload["metadata"]["acceptance_criteria"] == task.acceptance_criteria
    assert payload["metadata"]["target_agent"] == task.target_agent


def test_get_agent_task_returns_canonical_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_BUS_DISPATCH", "false")
    get_settings.cache_clear()
    _clear_agent_bus_client()
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
    get_settings.cache_clear()


def test_agent_task_submission_rejects_repository_not_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_BUS_DISPATCH", "false")
    get_settings.cache_clear()
    _clear_agent_bus_client()
    store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))

    with TestClient(app) as client:
        app.state.agent_task_store = store
        app.state.repository_registry = _registry(enabled=False)

        response = client.post("/api/v1/agent-tasks", json=_payload())

    assert response.status_code == 403
    assert response.json()["detail"] == "Repository is not orchestration-enabled."
    assert store.list_agent_tasks() == []
    get_settings.cache_clear()


def test_agent_tasks_are_discoverable_through_workflow_api(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_BUS_DISPATCH", "false")
    get_settings.cache_clear()
    _clear_agent_bus_client()
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
    get_settings.cache_clear()


def test_execution_result_updates_task_evidence_and_workflow_completed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_BUS_DISPATCH", "true")
    monkeypatch.setenv("AGENT_BUS_BASE_URL", "https://agent-bus.riseconnect.us")
    get_settings.cache_clear()
    store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))
    fake_bus = FakeAgentBusClient()

    with TestClient(app) as client:
        app.state.agent_task_store = store
        app.state.repository_registry = _registry()
        app.state.agent_bus_client = fake_bus

        created = client.post("/api/v1/agent-tasks", json=_payload())
        task_id = created.json()["task_id"]
        result = client.post(
            f"/api/v1/agent-tasks/{task_id}/execution-result",
            json={
                "agent_id": "codex-m2",
                "status": "completed",
                "commit_sha": "abc123",
                "branch": "agent-integration",
                "changed_files": ["app/example.py"],
                "evidence": {"tests": "not_run", "summary": "manual simulation"},
            },
        )
        workflow = client.get(f"/api/v1/workflows/wf-agent-task-{task_id}")

    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "completed"
    assert body["commit_sha"] == "abc123"
    assert body["branch"] == "agent-integration"
    assert body["changed_files"] == ["app/example.py"]
    assert body["execution_evidence"] == {"tests": "not_run", "summary": "manual simulation"}
    assert [event["event"] for event in body["lifecycle_events"]] == ["created", "queued", "assigned", "completed"]

    assert workflow.status_code == 200
    workflow_body = workflow.json()
    assert workflow_body["current_state"] == "COMPLETED"
    assert workflow_body["assigned_agent"] == "codex-m2"
    assert workflow_body["timeline"][-1]["canonical_state"] == "COMPLETED"
    assert workflow_body["timeline"][-1]["commit_sha"] == "abc123"
    _clear_agent_bus_client()
    get_settings.cache_clear()


def test_execution_result_rejects_wrong_agent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_BUS_DISPATCH", "false")
    get_settings.cache_clear()
    _clear_agent_bus_client()
    store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))
    task = create_agent_task(AgentTaskCreateRequest(**_payload()))
    store.save_agent_task(task)

    with TestClient(app) as client:
        app.state.agent_task_store = store
        app.state.repository_registry = _registry()

        response = client.post(
            f"/api/v1/agent-tasks/{task.task_id}/execution-result",
            json={"agent_id": "other-agent", "status": "completed", "changed_files": [], "evidence": {}},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Execution result agent_id does not match the task target_agent."
    get_settings.cache_clear()
