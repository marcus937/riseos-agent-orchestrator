from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent_task_dispatch import AgentTaskDispatchError, build_agent_bus_work_item_payload, dispatch_agent_task_to_agent_bus
from app.agent_tasks import AgentTaskCreateRequest, SQLiteAgentTaskStore, create_agent_task
from app.clients.agent_bus import AgentBusAPIError, AgentBusClient, MissingAgentBusBaseUrlError
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


@pytest.fixture(autouse=True)
def admin_token(monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_ADMIN_TOKEN", "admin-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    _clear_agent_bus_client()


def _auth() -> dict[str, str]:
    return {"X-Orchestrator-Admin-Token": "admin-token"}


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


def test_agent_task_endpoints_reject_unauthorized_access(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_BUS_DISPATCH", "false")
    get_settings.cache_clear()
    store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))
    task = create_agent_task(AgentTaskCreateRequest(**_payload()))
    store.save_agent_task(task)

    with TestClient(app) as client:
        app.state.agent_task_store = store
        app.state.repository_registry = _registry()
        created = client.post("/api/v1/agent-tasks", json=_payload())
        listed = client.get("/api/v1/agent-tasks")
        fetched = client.get(f"/api/v1/agent-tasks/{task.task_id}")
        result = client.post(
            f"/api/v1/agent-tasks/{task.task_id}/execution-result",
            json={"agent_id": "codex-m2", "status": "completed", "changed_files": [], "evidence": {}},
        )

    assert created.status_code == 401
    assert listed.status_code == 401
    assert fetched.status_code == 401
    assert result.status_code == 401
    assert store.get_agent_task(task.task_id).status == "queued"


def test_create_agent_task_persists_queued_task_and_lifecycle_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_BUS_DISPATCH", "false")
    get_settings.cache_clear()
    store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))

    with TestClient(app) as client:
        app.state.agent_task_store = store
        app.state.repository_registry = _registry()
        created = client.post("/api/v1/agent-tasks", json=_payload(), headers=_auth())
        listed = client.get("/api/v1/agent-tasks", headers=_auth())

    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "queued"
    assert body["target_agent"] == "codex-m2"
    assert body["task_id"].startswith("agtask-")
    assert listed.status_code == 200
    tasks = listed.json()
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == body["task_id"]
    assert [event["event"] for event in tasks[0]["lifecycle_events"]] == ["created", "queued"]
    reloaded = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db")).get_agent_task(body["task_id"])
    assert reloaded is not None
    assert reloaded.status == "queued"


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
        created = client.post("/api/v1/agent-tasks", json=_payload(), headers=_auth())

    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "assigned"
    assert fake_bus.payloads[0]["repository"] == "marcus937/riseos-agent-orchestrator"
    assert fake_bus.payloads[0]["owner_agent"] == "codex-m2"
    assert fake_bus.payloads[0]["review_agent"] == "bb2"
    assert fake_bus.payloads[0]["metadata"]["callback"] == {"method": "POST", "path": f"/api/v1/agent-tasks/{body['task_id']}/execution-result"}
    reloaded = store.get_agent_task(body["task_id"])
    assert reloaded is not None
    assert reloaded.status == "assigned"
    assert reloaded.agent_bus_work_item_id == "work-item-123"


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
    assert payload["metadata"]["source"] == "riseos-agent-orchestrator.agent_task"


def test_agent_bus_response_requires_work_item_id() -> None:
    class MissingWorkItemIdClient:
        async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {"id": "legacy-id"}

    task = create_agent_task(AgentTaskCreateRequest(**_payload()))
    import anyio
    with pytest.raises(AgentTaskDispatchError):
        anyio.run(dispatch_agent_task_to_agent_bus, task, MissingWorkItemIdClient())


def test_agent_bus_client_requires_base_url() -> None:
    client = AgentBusClient(base_url=None)
    import anyio
    with pytest.raises(MissingAgentBusBaseUrlError):
        anyio.run(client.create_work_item, {})


def test_agent_bus_client_uses_canonical_path_and_bearer_auth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"work_item_id": "work-1"})

    transport = httpx.MockTransport(handler)
    client = AgentBusClient(base_url="https://agent-bus.riseconnect.us", token="bus-token", http_client=httpx.AsyncClient(transport=transport))
    import anyio
    result = anyio.run(client.create_work_item, {"title": "Task"})

    assert result["work_item_id"] == "work-1"
    assert requests[0].url == "https://agent-bus.riseconnect.us/work-items"
    assert requests[0].headers["authorization"] == "Bearer bus-token"


def test_agent_bus_client_rejects_non_2xx_and_malformed_json() -> None:
    def non_2xx(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    def malformed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    import anyio
    bad_status = AgentBusClient(base_url="https://agent-bus", http_client=httpx.AsyncClient(transport=httpx.MockTransport(non_2xx)))
    bad_json = AgentBusClient(base_url="https://agent-bus", http_client=httpx.AsyncClient(transport=httpx.MockTransport(malformed)))
    with pytest.raises(AgentBusAPIError):
        anyio.run(bad_status.create_work_item, {})
    with pytest.raises(AgentBusAPIError):
        anyio.run(bad_json.create_work_item, {})


def test_get_agent_task_returns_canonical_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_BUS_DISPATCH", "false")
    get_settings.cache_clear()
    store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))
    task = create_agent_task(AgentTaskCreateRequest(**_payload()))
    store.save_agent_task(task)

    with TestClient(app) as client:
        app.state.agent_task_store = store
        app.state.repository_registry = _registry()
        response = client.get(f"/api/v1/agent-tasks/{task.task_id}", headers=_auth())

    assert response.status_code == 200
    assert response.json()["task_id"] == task.task_id


def test_agent_task_submission_rejects_repository_not_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_BUS_DISPATCH", "false")
    get_settings.cache_clear()
    store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))

    with TestClient(app) as client:
        app.state.agent_task_store = store
        app.state.repository_registry = _registry(enabled=False)
        response = client.post("/api/v1/agent-tasks", json=_payload(), headers=_auth())

    assert response.status_code == 403
    assert store.list_agent_tasks() == []


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
        created = client.post("/api/v1/agent-tasks", json=_payload(), headers=_auth())
        task_id = created.json()["task_id"]
        result = client.post(
            f"/api/v1/agent-tasks/{task_id}/execution-result",
            headers=_auth(),
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
    assert body["execution_evidence"] == {"tests": "not_run", "summary": "manual simulation"}
    assert workflow.status_code == 200
    assert workflow.json()["current_state"] == "COMPLETED"


def test_execution_result_rejects_wrong_agent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_BUS_DISPATCH", "false")
    get_settings.cache_clear()
    store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))
    task = create_agent_task(AgentTaskCreateRequest(**_payload()))
    store.save_agent_task(task)

    with TestClient(app) as client:
        app.state.agent_task_store = store
        app.state.repository_registry = _registry()
        response = client.post(
            f"/api/v1/agent-tasks/{task.task_id}/execution-result",
            headers=_auth(),
            json={"agent_id": "other-agent", "status": "completed", "changed_files": [], "evidence": {}},
        )

    assert response.status_code == 409
