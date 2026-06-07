import asyncio
import json
import logging
from typing import Any

from app.config import Settings, get_settings
from app.github_events import parse_github_event
from app.hermes_dispatch import InMemoryHermesDispatchRegistry, dispatch_hermes_runtime_validation


class FakeGitHubClient:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str]] = []

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        self.comments.append((repo_full_name, issue_number, body))
        return {"id": 1}

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.labels.append((repo_full_name, issue_number, label))
        return {"labels": [label]}


class FakeHermesClient:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {"status": "PASSED", "jobId": "issue-job-123"}
        self.jobs: list[tuple[str, str, dict[str, Any]]] = []

    async def post_job(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.jobs.append((base_url, token, payload))
        return self.response


class FakeSlackClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def post_message(self, channel: str, text: str) -> dict[str, Any]:
        self.messages.append((channel, text))
        return {"ok": True}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def settings(**overrides: Any) -> Settings:
    base = {
        "enable_github_writeback": True,
        "hermes_m2_base_url": "http://100.70.83.13:8787",
        "hermes_m2_token": "secret-token",
        "hermes_m2_enable_dispatch": True,
        "hermes_default_target": "https://preview.vercel.app",
    }
    base.update(overrides)
    return Settings(**base)


def issue_payload(*, labels: list[str] | None = None, action: str = "labeled", label: str = "playwright") -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
        "sender": {"login": "marcus"},
        "label": {"name": label},
        "issue": {
            "number": 53,
            "title": "Phase 5A direct Hermes dispatch validation trigger",
            "state": "open",
            "html_url": "https://github.com/marcus937/riseos-agent-orchestrator/issues/53",
            "labels": [{"name": item} for item in (labels or ["agent-ready", "testing", "playwright"])],
        },
    }


def log_events(records: list[logging.LogRecord]) -> list[dict[str, Any]]:
    return [json.loads(record.message) for record in records]


def assert_secret_absent(value: Any) -> None:
    serialized = json.dumps(value, default=str)
    assert "secret-token" not in serialized
    assert "url-secret" not in serialized
    assert "Bearer secret-token" not in serialized
    assert "X-Hermes-Token: secret-token" not in serialized


def test_issue_labeled_runtime_request_dispatches_hermes_m2_and_comments(caplog: Any) -> None:
    parsed = parse_github_event("issues", issue_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesClient()

    with caplog.at_level(logging.INFO, logger="riseos_agent_orchestrator"):
        result = run(
            dispatch_hermes_runtime_validation(
                parsed,
                settings(),
                github_client=github,
                hermes_client=hermes,
                registry=InMemoryHermesDispatchRegistry(),
            )
        )

    events = log_events(caplog.records)
    route_log = next(event for event in events if event["event"] == "hermes_route_evaluated")

    assert result.success is True
    assert result.status == "PASSED"
    assert result.correlation_id == "hermes-m2-marcus937-riseos-agent-orchestrator-issue-53-unknown"
    assert route_log["route"] == "issue_labeled_hermes_validate"
    assert route_log["labels_request_hermes"] is True
    assert hermes.jobs[0][2]["correlationId"] == result.correlation_id
    assert hermes.jobs[0][2]["payload"]["trigger"] == "issue_labeled_hermes_validate"
    assert hermes.jobs[0][2]["payload"]["repo"] == "marcus937/riseos-agent-orchestrator"
    assert hermes.jobs[0][2]["payload"]["subjectType"] == "issue"
    assert hermes.jobs[0][2]["payload"]["issueNumber"] == 53
    assert "prNumber" not in hermes.jobs[0][2]["payload"]
    assert hermes.jobs[0][2]["payload"]["screenshotName"] == "issue-53-validation.png"
    assert github.labels == [("marcus937/riseos-agent-orchestrator", 53, "agent-verified")]
    assert "Job ID: issue-job-123" in github.comments[0][2]
    assert "Correlation ID: hermes-m2-marcus937-riseos-agent-orchestrator-issue-53-unknown" in github.comments[0][2]
    for artifact in ["summary.json", "console.json", "network.json", "page.json", "screenshot.png"]:
        assert artifact in github.comments[0][2]
    assert_secret_absent(events)
    assert_secret_absent(result.model_dump())
    assert_secret_absent(github.comments)


def test_issue_labeled_testing_runtime_label_remains_backward_compatible(caplog: Any) -> None:
    parsed = parse_github_event("issues", issue_payload(labels=["agent-ready", "testing"], label="testing"))
    hermes = FakeHermesClient()

    with caplog.at_level(logging.INFO, logger="riseos_agent_orchestrator"):
        result = run(
            dispatch_hermes_runtime_validation(
                parsed,
                settings(enable_github_writeback=False),
                hermes_client=hermes,
                registry=InMemoryHermesDispatchRegistry(),
            )
        )

    events = log_events(caplog.records)
    route_log = next(event for event in events if event["event"] == "hermes_route_evaluated")

    assert result.attempted is True
    assert result.success is True
    assert len(hermes.jobs) == 1
    assert route_log["labels_request_hermes"] is True
    assert route_log["runtime_label_match"] is True
    assert_secret_absent(events)


def test_issue_labeled_dispatch_disabled_by_m2_flag_does_not_post_or_writeback() -> None:
    parsed = parse_github_event("issues", issue_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(hermes_m2_enable_dispatch=False),
            github_client=github,
            hermes_client=hermes,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.attempted is False
    assert result.skipped_reason == "HERMES_M2_ENABLE_DISPATCH=false."
    assert hermes.jobs == []
    assert github.comments == []
    assert github.labels == []
    assert_secret_absent(result.model_dump())


def test_issue_labeled_missing_m2_config_blocks_without_post_and_writes_back_when_enabled() -> None:
    parsed = parse_github_event("issues", issue_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(hermes_m2_token=None),
            github_client=github,
            hermes_client=hermes,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.attempted is True
    assert result.status == "BLOCKED"
    assert result.label == "agent-blocked"
    assert result.error == "Missing HERMES_M2_BASE_URL or HERMES_M2_TOKEN."
    assert hermes.jobs == []
    assert github.labels == [("marcus937/riseos-agent-orchestrator", 53, "agent-blocked")]
    assert "Status: BLOCKED" in github.comments[0][2]
    assert_secret_absent(result.model_dump())
    assert_secret_absent(github.comments)


def test_issue_labeled_duplicate_dispatch_skips_second_post_and_writeback() -> None:
    parsed = parse_github_event("issues", issue_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesClient()
    registry = InMemoryHermesDispatchRegistry()

    first = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(),
            github_client=github,
            hermes_client=hermes,
            registry=registry,
        )
    )
    second = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(),
            github_client=github,
            hermes_client=hermes,
            registry=registry,
        )
    )

    assert first.success is True
    assert second.attempted is False
    assert second.skipped_reason == "Hermes validation was already dispatched for this item commit and target."
    assert len(hermes.jobs) == 1
    assert len(github.comments) == 1
    assert github.labels == [("marcus937/riseos-agent-orchestrator", 53, "agent-verified")]
    assert_secret_absent(second.model_dump())


def test_issue_labeled_writeback_disabled_does_not_comment_or_label() -> None:
    parsed = parse_github_event("issues", issue_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(enable_github_writeback=False),
            github_client=github,
            hermes_client=hermes,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.success is True
    assert len(hermes.jobs) == 1
    assert github.comments == []
    assert github.labels == []
    assert result.comment is None
    assert_secret_absent(result.model_dump())


def test_hermes_slack_messages_use_hermes_destination_not_orchestrator_destination() -> None:
    parsed = parse_github_event("issues", issue_payload())
    slack = FakeSlackClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(
                enable_github_writeback=False,
                orchestrator_slack_webhook_url="https://hooks.slack.example/orchestrator",
                orchestrator_slack_channel="#jarvis-agent-orchestrator",
                hermes_slack_webhook_url="https://hooks.slack.example/hermes",
                hermes_slack_channel="#jarvis-hermes-runtime",
            ),
            slack_client=slack,
            hermes_client=FakeHermesClient(),
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.success is True
    assert len(slack.messages) == 2
    assert [channel for channel, _ in slack.messages] == ["#jarvis-hermes-runtime", "#jarvis-hermes-runtime"]
    assert "#jarvis-agent-orchestrator" not in json.dumps(slack.messages)
    assert_secret_absent(result.model_dump())


def test_hermes_and_orchestrator_slack_env_are_owned_separately(monkeypatch: Any) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("ORCHESTRATOR_SLACK_WEBHOOK_URL", "https://hooks.slack.example/orchestrator")
    monkeypatch.setenv("ORCHESTRATOR_SLACK_CHANNEL", "#jarvis-agent-orchestrator")
    monkeypatch.setenv("HERMES_SLACK_WEBHOOK_URL", "https://hooks.slack.example/hermes")
    monkeypatch.setenv("HERMES_SLACK_CHANNEL", "#jarvis-hermes-runtime")

    loaded = get_settings()

    assert loaded.orchestrator_slack_webhook_url == "https://hooks.slack.example/orchestrator"
    assert loaded.orchestrator_slack_channel == "#jarvis-agent-orchestrator"
    assert loaded.hermes_slack_webhook_url == "https://hooks.slack.example/hermes"
    assert loaded.hermes_slack_channel == "#jarvis-hermes-runtime"
    get_settings.cache_clear()


def test_issue_labeled_runtime_failure_applies_agent_blocked() -> None:
    parsed = parse_github_event("issues", issue_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesClient({"status": "FAILED", "jobId": "issue-job-failed"})

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(),
            github_client=github,
            hermes_client=hermes,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.status == "FAILED"
    assert result.label == "agent-blocked"
    assert github.labels == [("marcus937/riseos-agent-orchestrator", 53, "agent-blocked")]
    assert_secret_absent(result.model_dump())
    assert_secret_absent(github.comments)


def test_issue_labeled_without_required_label_set_skips_dispatch(caplog: Any) -> None:
    parsed = parse_github_event("issues", issue_payload(labels=["agent-ready"]))
    hermes = FakeHermesClient()

    with caplog.at_level(logging.INFO, logger="riseos_agent_orchestrator"):
        result = run(
            dispatch_hermes_runtime_validation(
                parsed,
                settings(),
                hermes_client=hermes,
                registry=InMemoryHermesDispatchRegistry(),
            )
        )

    events = log_events(caplog.records)
    route_log = next(event for event in events if event["event"] == "hermes_route_evaluated")

    assert result.attempted is False
    assert result.skipped_reason == "Event does not require Hermes runtime validation."
    assert hermes.jobs == []
    assert route_log["route"] is None
    assert route_log["labels_request_hermes"] is False
    assert_secret_absent(events)
    assert_secret_absent(result.model_dump())
