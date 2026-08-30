from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main as main_module
from app.agent_task_routes import _require_orchestration_enabled_repository
from app.config import Settings
from app.github_events import parse_github_event
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


def request_with_registry(registry: InMemoryRepositoryRegistry) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repository_registry=registry)))


def test_route_gate_auto_registers_missing_trusted_owner_repository() -> None:
    registry = InMemoryRepositoryRegistry()

    _require_orchestration_enabled_repository(
        "marcus937/jarvis-mission-control",
        request_with_registry(registry),
        Settings(trusted_repository_owner="marcus937"),
    )

    record = registry.get_repository_registry_record("marcus937/jarvis-mission-control")
    assert record is not None
    assert record.status == RepositoryStatus.ACTIVE
    assert record.archived is False
    assert record.orchestration_enabled is True


def test_route_gate_rejects_external_owner_without_registration() -> None:
    registry = InMemoryRepositoryRegistry()

    with pytest.raises(HTTPException) as exc_info:
        _require_orchestration_enabled_repository(
            "external/jarvis-mission-control",
            request_with_registry(registry),
            Settings(trusted_repository_owner="marcus937"),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Repository is not orchestration-enabled."
    assert registry.get_repository_registry_record("external/jarvis-mission-control") is None


def test_route_gate_rejects_archived_repository() -> None:
    registry = InMemoryRepositoryRegistry()
    registry.save_repository_registry_record(
        registry_record(
            "marcus937/archived",
            status=RepositoryStatus.ARCHIVED,
            archived=True,
            orchestration_enabled=False,
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        _require_orchestration_enabled_repository(
            "marcus937/archived",
            request_with_registry(registry),
            Settings(trusted_repository_owner="marcus937"),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Repository is archived."
    record = registry.get_repository_registry_record("marcus937/archived")
    assert record is not None
    assert record.archived is True
    assert record.orchestration_enabled is False


def test_route_gate_rejects_explicitly_disabled_repository() -> None:
    registry = InMemoryRepositoryRegistry()
    registry.save_repository_registry_record(registry_record("marcus937/disabled", orchestration_enabled=False))

    with pytest.raises(HTTPException) as exc_info:
        _require_orchestration_enabled_repository(
            "marcus937/disabled",
            request_with_registry(registry),
            Settings(trusted_repository_owner="marcus937"),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Repository is not orchestration-enabled."
    record = registry.get_repository_registry_record("marcus937/disabled")
    assert record is not None
    assert record.archived is False
    assert record.orchestration_enabled is False


def test_webhook_observation_still_does_not_auto_register_repository() -> None:
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
