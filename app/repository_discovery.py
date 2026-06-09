from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.config import Settings

REQUIRED_WEBHOOK_EVENTS = {
    "issues",
    "issue_comment",
    "label",
    "pull_request",
    "pull_request_review",
    "push",
}


class RepositoryStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    RENAMED = "renamed"


class WebhookStatus(StrEnum):
    HEALTHY = "healthy"
    MISSING = "missing"
    FAILED = "failed"
    SKIPPED = "skipped"


class RepositoryRegistryRecord(BaseModel):
    repo_full_name: str
    repo_id: int | None = None
    status: RepositoryStatus = RepositoryStatus.ACTIVE
    archived: bool = False
    previous_full_name: str | None = None
    default_branch: str | None = None
    orchestration_enabled: bool = True
    webhook_status: WebhookStatus = WebhookStatus.SKIPPED
    webhook_id: int | None = None
    webhook_events: list[str] = Field(default_factory=list)
    webhook_error: str | None = None
    last_event: str | None = None
    last_discovered_at: datetime
    last_work_item_generated_at: datetime | None = None
    onboarding_audit_log: list[str] = Field(default_factory=list)


class RepositoryDiscoveryResult(BaseModel):
    scanned_count: int = 0
    new_repositories: list[str] = Field(default_factory=list)
    archived_repositories: list[str] = Field(default_factory=list)
    renamed_repositories: list[str] = Field(default_factory=list)
    webhook_registered: list[str] = Field(default_factory=list)
    webhook_failures: dict[str, str] = Field(default_factory=dict)
    repositories: list[RepositoryRegistryRecord] = Field(default_factory=list)


class RepositoryRegistryStore(Protocol):
    def get_repository_registry_record(self, repo_full_name: str) -> RepositoryRegistryRecord | None:
        ...

    def get_repository_registry_record_by_id(self, repo_id: int) -> RepositoryRegistryRecord | None:
        ...

    def list_repository_registry_records(self) -> list[RepositoryRegistryRecord]:
        ...

    def save_repository_registry_record(self, record: RepositoryRegistryRecord) -> None:
        ...


class InMemoryRepositoryRegistry:
    def __init__(self) -> None:
        self._records: dict[str, RepositoryRegistryRecord] = {}
        self._record_names_by_id: dict[int, str] = {}

    def get_repository_registry_record(self, repo_full_name: str) -> RepositoryRegistryRecord | None:
        return self._records.get(repo_full_name)

    def get_repository_registry_record_by_id(self, repo_id: int) -> RepositoryRegistryRecord | None:
        record_name = self._record_names_by_id.get(repo_id)
        return self._records.get(record_name) if record_name else None

    def list_repository_registry_records(self) -> list[RepositoryRegistryRecord]:
        return sorted(self._records.values(), key=lambda record: record.repo_full_name.lower())

    def save_repository_registry_record(self, record: RepositoryRegistryRecord) -> None:
        self._records[record.repo_full_name] = record
        if record.repo_id is not None:
            self._record_names_by_id[record.repo_id] = record.repo_full_name

    def reset(self) -> None:
        self._records.clear()
        self._record_names_by_id.clear()


repository_registry = InMemoryRepositoryRegistry()


class SQLiteRepositoryRegistry:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS repository_registry (
                    repo_full_name TEXT PRIMARY KEY,
                    repo_id INTEGER,
                    status TEXT NOT NULL,
                    archived INTEGER NOT NULL,
                    previous_full_name TEXT,
                    default_branch TEXT,
                    orchestration_enabled INTEGER NOT NULL,
                    webhook_status TEXT NOT NULL,
                    webhook_id INTEGER,
                    webhook_events TEXT NOT NULL,
                    webhook_error TEXT,
                    last_event TEXT,
                    last_discovered_at TEXT NOT NULL,
                    last_work_item_generated_at TEXT,
                    onboarding_audit_log TEXT NOT NULL
                )
                """
            )

    def get_repository_registry_record(self, repo_full_name: str) -> RepositoryRegistryRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM repository_registry WHERE repo_full_name = ?",
                (repo_full_name,),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def get_repository_registry_record_by_id(self, repo_id: int) -> RepositoryRegistryRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM repository_registry WHERE repo_id = ? ORDER BY last_discovered_at DESC LIMIT 1",
                (repo_id,),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def list_repository_registry_records(self) -> list[RepositoryRegistryRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM repository_registry ORDER BY lower(repo_full_name)").fetchall()
        return [_record_from_row(row) for row in rows]

    def save_repository_registry_record(self, record: RepositoryRegistryRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO repository_registry (
                    repo_full_name,
                    repo_id,
                    status,
                    archived,
                    previous_full_name,
                    default_branch,
                    orchestration_enabled,
                    webhook_status,
                    webhook_id,
                    webhook_events,
                    webhook_error,
                    last_event,
                    last_discovered_at,
                    last_work_item_generated_at,
                    onboarding_audit_log
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.repo_full_name,
                    record.repo_id,
                    record.status.value,
                    1 if record.archived else 0,
                    record.previous_full_name,
                    record.default_branch,
                    1 if record.orchestration_enabled else 0,
                    record.webhook_status.value,
                    record.webhook_id,
                    json.dumps(record.webhook_events),
                    record.webhook_error,
                    record.last_event,
                    record.last_discovered_at.isoformat(),
                    record.last_work_item_generated_at.isoformat() if record.last_work_item_generated_at else None,
                    json.dumps(record.onboarding_audit_log),
                ),
            )


def build_repository_registry(settings: Settings) -> RepositoryRegistryStore:
    if not settings.orchestrator_db_path:
        return repository_registry
    try:
        return SQLiteRepositoryRegistry(settings.orchestrator_db_path)
    except (OSError, sqlite3.Error):
        return repository_registry


async def discover_repositories(
    owner: str,
    settings: Settings,
    github_client: Any,
    registry: RepositoryRegistryStore = repository_registry,
) -> RepositoryDiscoveryResult:
    repos = await github_client.list_owner_repositories(owner)
    now = datetime.now(UTC)
    result = RepositoryDiscoveryResult(scanned_count=len(repos))
    seen_repo_names: set[str] = set()

    for repo in repos:
        repo_full_name = str(repo.get("full_name") or "")
        if not repo_full_name:
            continue
        repo_id = _int_or_none(repo.get("id"))
        seen_repo_names.add(repo_full_name)
        existing = registry.get_repository_registry_record(repo_full_name)
        previous_by_id = registry.get_repository_registry_record_by_id(repo_id) if repo_id is not None else None
        previous_full_name = None
        status = RepositoryStatus.ARCHIVED if bool(repo.get("archived")) else RepositoryStatus.ACTIVE

        if existing is None and previous_by_id is not None and previous_by_id.repo_full_name != repo_full_name:
            previous_full_name = previous_by_id.repo_full_name
            status = RepositoryStatus.RENAMED
            result.renamed_repositories.append(repo_full_name)
        elif existing is None:
            result.new_repositories.append(repo_full_name)

        webhook = await _ensure_required_webhook(repo_full_name, settings, github_client)
        if webhook.status == WebhookStatus.HEALTHY and webhook.created:
            result.webhook_registered.append(repo_full_name)
        if webhook.error:
            result.webhook_failures[repo_full_name] = webhook.error

        audit_log = list((existing or previous_by_id).onboarding_audit_log) if (existing or previous_by_id) else []
        audit_log.append(_audit_message(status, webhook.status, now, previous_full_name=previous_full_name))
        record = RepositoryRegistryRecord(
            repo_full_name=repo_full_name,
            repo_id=repo_id,
            status=status,
            archived=bool(repo.get("archived")),
            previous_full_name=previous_full_name,
            default_branch=_str_or_none(repo.get("default_branch")),
            orchestration_enabled=status == RepositoryStatus.ACTIVE,
            webhook_status=webhook.status,
            webhook_id=webhook.webhook_id,
            webhook_events=sorted(webhook.events),
            webhook_error=webhook.error,
            last_event=existing.last_event if existing else None,
            last_discovered_at=now,
            last_work_item_generated_at=existing.last_work_item_generated_at if existing else None,
            onboarding_audit_log=audit_log[-25:],
        )
        registry.save_repository_registry_record(record)
        result.repositories.append(record)

    for record in registry.list_repository_registry_records():
        if record.repo_full_name in seen_repo_names or record.archived:
            continue
        archived = record.model_copy(
            update={
                "status": RepositoryStatus.ARCHIVED,
                "archived": True,
                "orchestration_enabled": False,
                "last_discovered_at": now,
                "onboarding_audit_log": [*record.onboarding_audit_log, f"{now.isoformat()} repository no longer returned by discovery"][-25:],
            }
        )
        registry.save_repository_registry_record(archived)
        result.archived_repositories.append(record.repo_full_name)

    return result


def repository_diagnostics(registry: RepositoryRegistryStore = repository_registry) -> list[dict[str, Any]]:
    return [
        {
            "repo": record.repo_full_name,
            "webhook_status": record.webhook_status.value,
            "last_event": record.last_event,
            "last_work_item_generated": record.last_work_item_generated_at.isoformat()
            if record.last_work_item_generated_at
            else None,
            "orchestration_enabled": record.orchestration_enabled,
            "onboarding_failures": [record.webhook_error] if record.webhook_error else [],
        }
        for record in registry.list_repository_registry_records()
    ]


class _WebhookResult(BaseModel):
    status: WebhookStatus
    webhook_id: int | None = None
    events: set[str] = Field(default_factory=set)
    error: str | None = None
    created: bool = False


async def _ensure_required_webhook(repo_full_name: str, settings: Settings, github_client: Any) -> _WebhookResult:
    callback_url = settings.github_webhook_callback_url
    if not callback_url:
        return _WebhookResult(status=WebhookStatus.SKIPPED, error="GITHUB_WEBHOOK_CALLBACK_URL is not configured.")

    try:
        hooks = await github_client.list_repository_webhooks(repo_full_name)
        for hook in hooks:
            events = set(str(event) for event in hook.get("events", []) if event)
            if REQUIRED_WEBHOOK_EVENTS.issubset(events):
                return _WebhookResult(
                    status=WebhookStatus.HEALTHY,
                    webhook_id=_int_or_none(hook.get("id")),
                    events=events,
                )

        created_hook = await github_client.create_repository_webhook(
            repo_full_name,
            callback_url=callback_url,
            secret=settings.github_webhook_secret,
            events=sorted(REQUIRED_WEBHOOK_EVENTS),
        )
    except Exception as exc:
        return _WebhookResult(status=WebhookStatus.FAILED, error=str(exc))

    return _WebhookResult(
        status=WebhookStatus.HEALTHY,
        webhook_id=_int_or_none(created_hook.get("id")),
        events=REQUIRED_WEBHOOK_EVENTS,
        created=True,
    )


def _audit_message(
    status: RepositoryStatus,
    webhook_status: WebhookStatus,
    timestamp: datetime,
    *,
    previous_full_name: str | None,
) -> str:
    rename = f" renamed_from={previous_full_name}" if previous_full_name else ""
    return f"{timestamp.isoformat()} status={status.value} webhook_status={webhook_status.value}{rename}"


def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record_from_row(row: sqlite3.Row) -> RepositoryRegistryRecord:
    data = dict(row)
    data["archived"] = bool(data["archived"])
    data["orchestration_enabled"] = bool(data["orchestration_enabled"])
    data["webhook_events"] = json.loads(data["webhook_events"] or "[]")
    data["onboarding_audit_log"] = json.loads(data["onboarding_audit_log"] or "[]")
    return RepositoryRegistryRecord.model_validate(data)
