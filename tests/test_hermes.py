import asyncio
from typing import Any

from app.config import Settings
from app.github_events import parse_github_event
from app.hermes import (
    LABEL_AGENT_BLOCKED,
    LABEL_AGENT_REVISIONS,
    LABEL_AGENT_VERIFIED,
    InMemoryHermesDispatchRegistry,
    build_hermes_comment,
    build_hermes_correlation_id,
    build_hermes_validation_request,
    dispatch_hermes_validation,
    hermes_label_for_status,
    is_allowed_hermes_target,
    should_dispatch_hermes,
)
from app.review_queue import process_review_work_item, review_work_item_from_parsed


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeHermesClient:
    async def status(self) -> dict[str, Any]:
        return {"node": "m2-hermes"}

    async def create_job(self, request: Any) -> dict[str, Any]:
        self.request = request
        return {"jobId": "job-123"}

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return {"jobId": job_id, "status": "PASS"}

    async def get_evidence(self, job_id: str) -> dict[str, Any]:
        return {
            "status": "PASS",
            "summary": "Example target loaded and screenshots were captured.",
            "files": ["desktop.png", "mobile.png"],
            "screenshotName": "pr-16-validation.png",
        }


class FailingHermesClient(FakeHermesClient):
    async def create_job(self, request: Any) -> dict[str, Any]:
        raise RuntimeError("Hermes offline")


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


class FakeSlackClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def post_message(self, *, channel: str, text: str) -> None:
        self.messages.append((channel, text))


def pr_event(labels: list[str] | None = None, *, comment_body: str | None = None) -> Any:
    if comment_body is not None:
        return parse_github_event(
            "issue_comment",
            {
                "action": "created",
                "repository": {"full_name": "riseos/example"},
                "issue": {"number": 16, "pull_request": {"url": "https://api.github.com/pulls/16"}, "labels": [{"name": label} for label in labels or []]},
                "comment": {"body": comment_body},
            },
        )

    return parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {
                "number": 16,
                "head": {"ref": "agent-integration", "sha": "10e7785abcdef"},
                "labels": [{"name": label} for label in labels or []],
            },
        },
    )


def test_label_trigger_detection_requires_runtime_and_review_label() -> None:
    assert should_dispatch_hermes(pr_event(["runtime-agent", "bb-review-needed"])) is True
    assert should_dispatch_hermes(pr_event(["playwright", "agent-review"])) is True
    assert should_dispatch_hermes(pr_event(["runtime-agent"])) is False
    assert should_dispatch_hermes(pr_event(["bb-review-needed"])) is False


def test_comment_trigger_detection_on_pr_comment() -> None:
    assert should_dispatch_hermes(pr_event(comment_body="/hermes validate")) is True
    assert should_dispatch_hermes(pr_event(comment_body="needs hermes validation please")) is True
    assert should_dispatch_hermes(pr_event(comment_body="ordinary review comment")) is False


def test_payload_includes_pr_commit_branch_and_stable_correlation() -> None:
    parsed = pr_event(["runtime-agent", "bb-review-needed"])
    item = review_work_item_from_parsed(parsed)
    settings = Settings(hermes_default_target="https://example.com")

    request = build_hermes_validation_request(item, settings)

    assert request.type == "playwright"
    assert request.targetUrl == "https://example.com"
    assert request.correlationId == "hermes-riseos-example-pr-16-10e7785"
    assert request.payload["repo"] == "riseos/example"
    assert request.payload["prNumber"] == 16
    assert request.payload["commitSha"] == "10e7785abcdef"
    assert request.payload["branch"] == "agent-integration"
    assert build_hermes_correlation_id(item) == request.correlationId


def test_duplicate_dispatch_is_suppressed_for_same_correlation() -> None:
    parsed = pr_event(["runtime-agent", "bb-review-needed"])
    response = process_review_work_item(review_work_item_from_parsed(parsed))
    settings = Settings(hermes_enable_dispatch=True, hermes_base_url="http://100.70.83.13:8787", hermes_token="secret")
    registry = InMemoryHermesDispatchRegistry()

    first = run(dispatch_hermes_validation(response, settings, hermes_client=FakeHermesClient(), registry=registry))
    second = run(dispatch_hermes_validation(response, settings, hermes_client=FakeHermesClient(), registry=registry))

    assert first.success is True
    assert second.attempted is False
    assert second.skipped_reason == "Hermes dispatch already claimed for this PR commit."


def test_successful_dispatch_posts_comment_label_and_slack_message() -> None:
    parsed = pr_event(["runtime-agent", "bb-review-needed"])
    response = process_review_work_item(review_work_item_from_parsed(parsed))
    settings = Settings(
        hermes_enable_dispatch=True,
        hermes_base_url="http://100.70.83.13:8787",
        hermes_token="super-secret-token",
        slack_channel="#jarvis-agent-orchestrator",
    )
    github = FakeGitHubClient()
    slack = FakeSlackClient()

    result = run(
        dispatch_hermes_validation(
            response,
            settings,
            hermes_client=FakeHermesClient(),
            github_client=github,
            slack_client=slack,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.success is True
    assert result.job_id == "job-123"
    assert github.labels == [("riseos/example", 16, LABEL_AGENT_VERIFIED)]
    assert "## Hermes Runtime Validation" in github.comments[0][2]
    assert "super-secret-token" not in github.comments[0][2]
    assert slack.messages[0][0] == "#jarvis-agent-orchestrator"
    assert "Hermes validation complete" in slack.messages[0][1]


def test_failed_dispatch_posts_blocked_label() -> None:
    parsed = pr_event(["runtime-agent", "bb-review-needed"])
    response = process_review_work_item(review_work_item_from_parsed(parsed))
    settings = Settings(hermes_enable_dispatch=True, hermes_base_url="http://100.70.83.13:8787", hermes_token="secret")
    github = FakeGitHubClient()

    result = run(
        dispatch_hermes_validation(
            response,
            settings,
            hermes_client=FailingHermesClient(),
            github_client=github,
            registry=InMemoryHermesDispatchRegistry(),
        )
    )

    assert result.success is False
    assert "Hermes offline" in result.error
    assert github.labels == [("riseos/example", 16, LABEL_AGENT_BLOCKED)]


def test_result_labels_and_target_allowlist() -> None:
    assert hermes_label_for_status("PASS") == LABEL_AGENT_VERIFIED
    assert hermes_label_for_status("needs_changes") == LABEL_AGENT_REVISIONS
    assert hermes_label_for_status("FAIL") == LABEL_AGENT_BLOCKED
    assert is_allowed_hermes_target("https://example.com") is True
    assert is_allowed_hermes_target("https://preview.vercel.app") is True
    assert is_allowed_hermes_target("https://production.example.org") is False


def test_comment_body_contains_required_evidence_sections() -> None:
    body = build_hermes_comment(
        result=type(
            "Result",
            (),
            {
                "status": "PASS",
                "success": True,
                "job_id": "job-1",
                "node": "m2-hermes",
                "target_url": "https://example.com",
                "correlation_id": "hermes-riseos-example-pr-16-10e7785",
                "evidence": {"files": ["desktop.png"], "summary": "ok", "screenshotName": "desktop.png"},
            },
        )()
    )

    for section in ["VERIFIED", "ASSUMED", "UNVERIFIED", "Evidence files", "Evidence API response"]:
        assert section in body
