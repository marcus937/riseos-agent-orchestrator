import asyncio
from typing import Any

from app.config import Settings
from app.github_events import GitHubEventType, parse_github_event
from app.hermes_dispatch import (
    CANONICAL_HERMES_TRIGGER_LABELS,
    InMemoryHermesDispatchRegistry,
    build_hermes_job_payload,
    build_hermes_pr_comment,
    build_hermes_slack_message,
    dispatch_hermes_runtime_validation,
)


class FakeSlackClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def post_message(self, *, channel: str, text: str) -> None:
        self.messages.append((channel, text))


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
        self.response = response or {"status": "PASSED", "jobId": "job-123"}
        self.jobs: list[tuple[str, str, dict[str, Any]]] = []

    async def post_job(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.jobs.append((base_url, token, payload))
        return self.response


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def settings(**overrides: Any) -> Settings:
    base = {
        "slack_channel": "#jarvis-agent-orchestrator",
        "enable_github_writeback": True,
        "hermes_m2_base_url": "http://100.70.83.13:8787",
        "hermes_m2_token": "secret-token",
        "hermes_m2_enable_dispatch": True,
        "hermes_default_target": "https://preview.vercel.app",
    }
    base.update(overrides)
    return Settings(**base)


def pr_payload(*, labels: list[str] | None = None, action: str = "labeled", label: str = "playwright") -> dict[str, Any]:
    label_names = labels if labels is not None else ["runtime-agent", "playwright", "bb-review-needed"]
    return {
        "action": action,
        "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
        "sender": {"login": "marcus"},
        "label": {"name": label},
        "pull_request": {
            "number": 51,
            "head": {"ref": "agent-integration", "sha": "abcdef1234567890"},
            "base": {"ref": "main"},
            "labels": [{"name": item} for item in label_names],
        },
    }


def test_pr_label_runtime_request_dispatches_hermes_m2() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    slack = FakeSlackClient()
    github = FakeGitHubClient()
    hermes = FakeHermesClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(slack_webhook_url="https://hooks.slack.test/services/test"),
            slack_client=slack,
            github_client=github,
            hermes_client=hermes,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.success is True
    assert result.status == "PASSED"
    assert hermes.jobs[0][0] == "http://100.70.83.13:8787"
    assert hermes.jobs[0][1] == "secret-token"
    assert hermes.jobs[0][2]["payload"]["repo"] == "marcus937/riseos-agent-orchestrator"
    assert hermes.jobs[0][2]["payload"]["prNumber"] == 51
    assert hermes.jobs[0][2]["payload"]["branch"] == "agent-integration"
    assert github.labels == [("marcus937/riseos-agent-orchestrator", 51, "agent-verified")]
    assert "secret-token" not in github.comments[0][2]
    assert "Hermes validation requested" in slack.messages[0][1]
    assert "Hermes validation complete" in slack.messages[1][1]


def test_agent_integration_pr_opened_applies_hermes_labels_and_dispatches() -> None:
    parsed = parse_github_event("pull_request", pr_payload(action="opened", labels=[]))
    github = FakeGitHubClient()
    hermes = FakeHermesClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(),
            github_client=github,
            hermes_client=hermes,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    expected_trigger_labels = [
        ("marcus937/riseos-agent-orchestrator", 51, label) for label in CANONICAL_HERMES_TRIGGER_LABELS
    ]
    assert result.success is True
    assert hermes.jobs[0][2]["payload"]["trigger"] == "pull_request_opened_circuit_hermes"
    assert hermes.jobs[0][2]["payload"]["labels"] == sorted(CANONICAL_HERMES_TRIGGER_LABELS)
    assert github.labels == [*expected_trigger_labels, ("marcus937/riseos-agent-orchestrator", 51, "agent-verified")]


def test_dispatch_disabled_skips_without_side_effects() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
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
    assert github.comments == []
    assert github.labels == []
    assert hermes.jobs == []


def test_pr_comment_hermes_validate_dispatches() -> None:
    parsed = parse_github_event(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
            "issue": {
                "number": 51,
                "title": "Runtime work",
                "state": "open",
                "html_url": "https://github.com/marcus937/riseos-agent-orchestrator/pull/51",
                "pull_request": {"url": "https://api.github.com/repos/marcus937/riseos-agent-orchestrator/pulls/51"},
                "labels": [{"name": "bb2-blocked"}],
            },
            "comment": {"body": "/hermes validate\nCommit SHA: abcdef1234567890"},
        },
    )
    hermes = FakeHermesClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(),
            hermes_client=hermes,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.attempted is True
    assert hermes.jobs[0][2]["correlationId"] == "hermes-m2-marcus937-riseos-agent-orchestrator-pr-51-abcdef1"


def test_bb2_blocked_pr_does_not_auto_dispatch_without_explicit_comment() -> None:
    parsed = parse_github_event(
        "pull_request",
        pr_payload(labels=["runtime-agent", "playwright", "bb-review-needed", "bb2-blocked"]),
    )

    result = run(dispatch_hermes_runtime_validation(parsed, settings(), registry=InMemoryHermesDispatchRegistry()))

    assert result.attempted is False
    assert result.skipped_reason == "Event does not require Hermes runtime validation."


def test_duplicate_dispatch_is_suppressed_for_same_pr_commit_target_node() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    registry = InMemoryHermesDispatchRegistry()
    hermes = FakeHermesClient()

    first = run(dispatch_hermes_runtime_validation(parsed, settings(), hermes_client=hermes, registry=registry))
    second = run(dispatch_hermes_runtime_validation(parsed, settings(), hermes_client=hermes, registry=registry))

    assert first.attempted is True
    assert second.attempted is False
    assert second.skipped_reason == "Hermes validation was already dispatched for this PR commit and target."
    assert len(hermes.jobs) == 1


def test_missing_hermes_config_blocks_and_comments() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(hermes_m2_token=None),
            github_client=github,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.status == "BLOCKED"
    assert result.label == "agent-blocked"
    assert "Missing HERMES_M2_BASE_URL or HERMES_M2_TOKEN" in result.error
    assert "Status: BLOCKED" in github.comments[0][2]
    assert github.labels == [("marcus937/riseos-agent-orchestrator", 51, "agent-blocked")]


def test_placeholder_default_target_blocks_validation_writeback() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(hermes_default_target="https://example.com"),
            github_client=github,
            hermes_client=hermes,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.status == "BLOCKED"
    assert "placeholder" in result.error
    assert hermes.jobs == []
    assert github.labels == [("marcus937/riseos-agent-orchestrator", 51, "agent-blocked")]


def test_failed_hermes_response_requests_revisions() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesClient({"status": "FAILED", "jobId": "job-fail"})

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
    assert result.label == "agent-revisions"
    assert github.labels == [("marcus937/riseos-agent-orchestrator", 51, "agent-revisions")]


def test_writeback_disabled_posts_no_github_comment_or_label() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(enable_github_writeback=False),
            github_client=github,
            hermes_client=FakeHermesClient(),
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.success is True
    assert github.comments == []
    assert github.labels == []


def test_slack_and_pr_comment_payloads_include_required_sections() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(),
            hermes_client=FakeHermesClient({"status": "PASSED", "jobId": "job-ok"}),
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    slack_message = build_hermes_slack_message(parsed, result, settings())
    comment = build_hermes_pr_comment(parsed, result, settings())

    assert "Hermes validation complete" in slack_message
    assert "Evidence: summary.json, logs.json, console.json, network.json, page.json, screenshot.png" in slack_message
    assert "not merge approval" in comment
    for section in ["Hermes Runtime Validation", "VERIFIED", "ASSUMED", "UNVERIFIED"]:
        assert section in comment


def test_dgx_labeled_request_returns_not_enabled_result_when_disabled() -> None:
    parsed = parse_github_event(
        "pull_request",
        pr_payload(labels=["dgx", "runtime-agent", "evidence", "bb-review-needed"]),
    )
    github = FakeGitHubClient()
    hermes = FakeHermesClient()

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(hermes_dgx_enable_dispatch=False),
            github_client=github,
            hermes_client=hermes,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.hermes_node == "DGX"
    assert result.attempted is False
    assert result.skipped_reason == "HERMES_DGX_ENABLE_DISPATCH=false."
    assert github.labels == []
    assert hermes.jobs == []


def test_pull_request_review_submitted_with_runtime_labels_dispatches() -> None:
    parsed = parse_github_event("pull_request_review", pr_payload(action="submitted"))

    assert parsed.event_type == GitHubEventType.PULL_REQUEST_REVIEW

    result = run(
        dispatch_hermes_runtime_validation(
            parsed,
            settings(),
            hermes_client=FakeHermesClient(),
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.attempted is True


def test_build_job_payload_can_use_phase_four_placeholder_target() -> None:
    parsed = parse_github_event("pull_request", pr_payload())

    payload = build_hermes_job_payload(parsed, settings(hermes_default_target="https://example.com"), route="pull_request_labeled")

    assert payload["type"] == "playwright"
    assert payload["targetUrl"] == "https://example.com"
    assert payload["payload"]["screenshotName"] == "pr-51-validation.png"
