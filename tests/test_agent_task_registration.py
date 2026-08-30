from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings, get_settings
from app.github_events import parse_github_event
from app.main import app
from app.repository_discovery import InMemoryRepositoryRegistry, RepositoryRegistryRecord, RepositoryStatus


def registry_record(
    repo_full_name: str,
    *,
    status: RepositoryStatus = RepositoryStatus.ACTIVE,
    archived: bool = False,
    orchestration_enabled: bool = True,
) -> RepositoryRegistryRecord:
    return RepositoryRegistryRecord(
        repo_full_name=repo_full_name,
        status=status,
        archived=archived,
        orchestration_enabled=orchestration_enabled,
        last_discovered_at=datetime.now(UTC),
    )


def agent_task_payload(repo_full_name: str) -> dict[str, object]:
    return {
        "repo_full_name": repo_full_name,
        "title": "Queued agent task",
        "issue_number": 42,
        "labels": ["agent-task", "agent-ready"],
    }


def configured_client(registry: InMemoryRepositoryRegistry, *, admin_token: str = "admin-token") -> TestClient:
    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: Settings(orchestrator_admin_token=admin_token)
    client = TestClient(app)
    main_module.app.state.repository_registry = registry
    return client


def test_authenticated_agent_task_for_trusted_owner_auto_registers_and_allows_creation() -> None:
    registry = InMemoryRepositoryRegistry()
    client = configured_client(registry)

    response = client.post(
        "/api/v1/agent-tasks",
        json=agent_task_payload("marcus937/jarvis-mission-control"),
        headers={"X-Orchestrator-Admin-Token": "admin-token"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "accepted": True,
        "repo_full_name": "marcus937/jarvis-mission-control",
        "orchestration_enabled": True,
        "auto_registered": True,
        "issue_number": 42,
    }
    record = registry.get_repository_registry_record("marcus937/jarvis-mission-control")
    assert record is not None
    assert record.status == RepositoryStatus.ACTIVE
    assert record.archived is False
    assert record.orchestration_enabled is True


def test_webhook_observation_alone_does_not_auto_register() -> None:
    registry = InMemoryRepositoryRegistry()
    main_module.app.state.repository_registry = registry
    parsed = parse_github_event(
        "issues",
        {
            "action": "opened",
            "repository": {"full_name": "marcus937/Project-Jarvis"},
            "sender": {"login": "marcus"},
            "issue": {
                "number": 1,
                "title": "Observed only",
                "state": "open",
                "html_url": "https://github.com/marcus937/Project-Jarvis/issues/1",
                "labels": [{"name": "agent-ready"}],
            },
        },
    )

    main_module._record_repository_event(parsed, work_item_created=True)

    assert registry.get_repository_registry_record("marcus937/Project-Jarvis") is None


def test_webhook_observation_alone_does_not_enable_existing_disabled_repository() -> None:
    registry = InMemoryRepositoryRegistry()
    registry.save_repository_registry_record(registry_record("marcus937/disabled", orchestration_enabled=False))
    main_module.app.state.repository_registry = registry
    parsed = parse_github_event(
        "issues",
        {
            "action": "opened",
            "repository": {"full_name": "marcus937/disabled"},
            "sender": {"login": "marcus"},
            "issue": {
                "number": 2,
                "title": "Disabled repo event",
                "state": "open",
                "html_url": "https://github.com/marcus937/disabled/issues/2",
                "labels": [{"name": "agent-ready"}],
            },
        },
    )

    main_module._record_repository_event(parsed, work_item_created=True)

    record = registry.get_repository_registry_record("marcus937/disabled")
    assert record is not None
    assert record.orchestration_enabled is False
    assert record.last_event is not None
    assert record.last_work_item_generated_at is not None


def test_external_owner_agent_task_is_rejected_and_not_registered() -> None:
    registry = InMemoryRepositoryRegistry()
    client = configured_client(registry)

    response = client.post(
        "/api/v1/agent-tasks",
        json=agent_task_payload("external/Project-Jarvis"),
        headers={"X-Orchestrator-Admin-Token": "admin-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Repository is not orchestration-enabled."
    assert registry.get_repository_registry_record("external/Project-Jarvis") is None


def test_archived_repository_agent_task_is_rejected() -> None:
    registry = InMemoryRepositoryRegistry()
    registry.save_repository_registry_record(
        registry_record(
            "marcus937/archived",
            status=RepositoryStatus.ARCHIVED,
            archived=True,
            orchestration_enabled=False,
        )
    )
    client = configured_client(registry)

    response = client.post(
        "/api/v1/agent-tasks",
        json=agent_task_payload("marcus937/archived"),
        headers={"X-Orchestrator-Admin-Token": "admin-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Repository is archived."
    record = registry.get_repository_registry_record("marcus937/archived")
    assert record is not None
    assert record.archived is True
    assert record.orchestration_enabled is False


def test_explicitly_disabled_repository_agent_task_is_rejected() -> None:
    registry = InMemoryRepositoryRegistry()
    registry.save_repository_registry_record(registry_record("marcus937/disabled", orchestration_enabled=False))
    client = configured_client(registry)

    response = client.post(
        "/api/v1/agent-tasks",
        json=agent_task_payload("marcus937/disabled"),
        headers={"X-Orchestrator-Admin-Token": "admin-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Repository is not orchestration-enabled."
    record = registry.get_repository_registry_record("marcus937/disabled")
    assert record is not None
    assert record.archived is False
    assert record.orchestration_enabled is False
