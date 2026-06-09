import asyncio

from app.config import Settings
from app.github_events import parse_github_event
from app.repository_discovery import (
    InMemoryRepositoryRegistry,
    REQUIRED_WEBHOOK_EVENTS,
    RepositoryStatus,
    SQLiteRepositoryRegistry,
    WebhookStatus,
    discover_repositories,
    repository_diagnostics,
)
from app.slack_issue_dispatch import InMemoryDispatchedIssueRegistry, dispatch_ready_issue_to_slack


class FakeGitHubClient:
    def __init__(self, *, repos=None, hooks=None) -> None:
        self.repos = repos or []
        self.hooks = hooks or {}
        self.created_hooks = []

    async def list_owner_repositories(self, owner: str):
        return self.repos

    async def list_repository_webhooks(self, repo_full_name: str):
        return self.hooks.get(repo_full_name, [])

    async def create_repository_webhook(self, repo_full_name: str, *, callback_url: str, secret: str, events: list[str]):
        self.created_hooks.append(
            {
                "repo_full_name": repo_full_name,
                "callback_url": callback_url,
                "secret": secret,
                "events": events,
            }
        )
        return {"id": 9001, "events": events}


class FakeSlackClient:
    def __init__(self) -> None:
        self.messages = []

    async def post_message(self, *, channel: str, text: str) -> None:
        self.messages.append((channel, text))


def run(coro):
    return asyncio.run(coro)


def test_discovery_detects_new_repository_and_registers_missing_webhook() -> None:
    registry = InMemoryRepositoryRegistry()
    github = FakeGitHubClient(
        repos=[{"id": 1, "full_name": "marcus937/jarvis-agent-bus-mcp", "archived": False, "default_branch": "main"}],
        hooks={"marcus937/jarvis-agent-bus-mcp": []},
    )
    settings = Settings(
        github_webhook_secret="secret",
        github_webhook_callback_url="https://orchestrator.example/webhooks/github",
    )

    result = run(discover_repositories("marcus937", settings, github, registry))

    assert result.scanned_count == 1
    assert result.new_repositories == ["marcus937/jarvis-agent-bus-mcp"]
    assert result.webhook_registered == ["marcus937/jarvis-agent-bus-mcp"]
    assert github.created_hooks[0]["events"] == sorted(REQUIRED_WEBHOOK_EVENTS)
    record = registry.get_repository_registry_record("marcus937/jarvis-agent-bus-mcp")
    assert record is not None
    assert record.webhook_status == WebhookStatus.HEALTHY
    assert record.orchestration_enabled is True


def test_discovery_validates_existing_required_webhook_without_creating() -> None:
    registry = InMemoryRepositoryRegistry()
    github = FakeGitHubClient(
        repos=[{"id": 2, "full_name": "marcus937/existing", "archived": False}],
        hooks={"marcus937/existing": [{"id": 7, "events": sorted(REQUIRED_WEBHOOK_EVENTS)}]},
    )

    result = run(
        discover_repositories(
            "marcus937",
            Settings(github_webhook_callback_url="https://orchestrator.example/webhooks/github"),
            github,
            registry,
        )
    )

    assert result.webhook_registered == []
    assert github.created_hooks == []
    assert result.repositories[0].webhook_id == 7
    assert result.repositories[0].webhook_status == WebhookStatus.HEALTHY


def test_discovery_marks_renamed_repository_by_stable_id() -> None:
    registry = InMemoryRepositoryRegistry()
    first = FakeGitHubClient(repos=[{"id": 3, "full_name": "marcus937/old-name", "archived": False}])
    settings = Settings(github_webhook_callback_url="https://orchestrator.example/webhooks/github")
    run(discover_repositories("marcus937", settings, first, registry))

    second = FakeGitHubClient(repos=[{"id": 3, "full_name": "marcus937/new-name", "archived": False}])
    result = run(discover_repositories("marcus937", settings, second, registry))

    assert result.renamed_repositories == ["marcus937/new-name"]
    renamed = registry.get_repository_registry_record("marcus937/new-name")
    assert renamed is not None
    assert renamed.status == RepositoryStatus.RENAMED
    assert renamed.previous_full_name == "marcus937/old-name"


def test_discovery_marks_missing_repository_archived() -> None:
    registry = InMemoryRepositoryRegistry()
    settings = Settings(github_webhook_callback_url="https://orchestrator.example/webhooks/github")
    run(discover_repositories("marcus937", settings, FakeGitHubClient(repos=[{"id": 4, "full_name": "marcus937/active"}]), registry))

    result = run(discover_repositories("marcus937", settings, FakeGitHubClient(repos=[]), registry))

    assert result.archived_repositories == ["marcus937/active"]
    archived = registry.get_repository_registry_record("marcus937/active")
    assert archived is not None
    assert archived.archived is True
    assert archived.orchestration_enabled is False


def test_sqlite_repository_registry_persists_records(tmp_path) -> None:
    db_path = tmp_path / "orchestrator.db"
    settings = Settings(github_webhook_callback_url="https://orchestrator.example/webhooks/github")
    registry = SQLiteRepositoryRegistry(str(db_path))
    github = FakeGitHubClient(repos=[{"id": 5, "full_name": "marcus937/persisted", "archived": False}])

    run(discover_repositories("marcus937", settings, github, registry))
    reloaded = SQLiteRepositoryRegistry(str(db_path)).get_repository_registry_record("marcus937/persisted")

    assert reloaded is not None
    assert reloaded.repo_id == 5
    assert reloaded.repo_full_name == "marcus937/persisted"


def test_repository_diagnostics_include_health_dashboard_shape() -> None:
    registry = InMemoryRepositoryRegistry()
    settings = Settings(github_webhook_callback_url="https://orchestrator.example/webhooks/github")
    github = FakeGitHubClient(repos=[{"id": 6, "full_name": "marcus937/dashboard", "archived": False}])
    run(discover_repositories("marcus937", settings, github, registry))

    diagnostics = repository_diagnostics(registry)

    assert diagnostics[0]["repo"] == "marcus937/dashboard"
    assert diagnostics[0]["webhook_status"] == "healthy"
    assert diagnostics[0]["orchestration_enabled"] is True
    assert "last_event" in diagnostics[0]
    assert "last_work_item_generated" in diagnostics[0]
    assert "onboarding_failures" in diagnostics[0]


def test_discovered_repository_can_dispatch_agent_ready_issue_to_slack() -> None:
    parsed = parse_github_event(
        "issues",
        {
            "action": "labeled",
            "repository": {"full_name": "marcus937/newly-discovered"},
            "sender": {"login": "marcus"},
            "label": {"name": "agent-ready"},
            "issue": {
                "number": 2,
                "title": "New repo task",
                "state": "open",
                "html_url": "https://github.com/marcus937/newly-discovered/issues/2",
                "labels": [{"name": "agent-ready"}],
            },
        },
    )
    slack = FakeSlackClient()

    result = run(
        dispatch_ready_issue_to_slack(
            parsed,
            Settings(slack_webhook_url="https://hooks.slack.test/services/test"),
            client=slack,
            registry=InMemoryDispatchedIssueRegistry(),
            approved_repositories={"marcus937/newly-discovered"},
        )
    )

    assert result.success is True
    assert "Repo: marcus937/newly-discovered" in slack.messages[0][1]
