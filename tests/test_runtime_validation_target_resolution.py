from typing import Any

import anyio

from app.circuit_runtime_validation import RuntimeValidationRequest, _build_runtime_payload, runtime_validation_store
from app.config import Settings
from app.github_events import parse_github_event
from app.hermes_contract import runtime_validation_request_from_parsed


class FakePreviewClient:
    def __init__(self, statuses: list[dict[str, Any]] | None = None, checks: list[dict[str, Any]] | None = None) -> None:
        self.statuses = statuses or []
        self.checks = checks or []

    async def list_commit_statuses(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.statuses

    async def list_check_runs_for_ref(self, repo_full_name: str, ref: str) -> list[dict[str, Any]]:
        return self.checks


class FailingHermesClient:
    async def post_runtime_validation(self, *args: object, **kwargs: object) -> dict[str, Any]:
        raise AssertionError("pending preview validation must not dispatch Hermes")

    async def collect_evidence(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("pending preview validation must not collect evidence")

    async def aclose(self) -> None:
        pass


def _settings() -> Settings:
    return Settings(
        github_webhook_secret="test-secret",
        orchestrator_admin_token="admin-token",
        enable_runtime_validation_review_bridge=True,
        hermes_m2_enable_dispatch=True,
        hermes_m2_base_url="https://hermes.example.test",
        hermes_m2_token="hermes-token",
        hermes_default_target="https://apple.com",
    )


def _payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "number": 91,
        "repository": {"full_name": "marcus937/jarvis-mission-control"},
        "pull_request": {
            "number": 91,
            "head": {"sha": "abc123", "ref": "codex-m2/workflow-wf-1", "repo": {"full_name": "marcus937/jarvis-mission-control"}},
            "base": {"ref": "agent-integration", "repo": {"full_name": "marcus937/jarvis-mission-control"}},
            "labels": [],
        },
    }


async def _build_request(client: FakePreviewClient):
    parsed = parse_github_event("pull_request", _payload())
    return await runtime_validation_request_from_parsed(parsed, _settings(), github_client=client)


async def _trigger_validation(request):
    return await runtime_validation_store.trigger(request, _settings())


def test_runtime_validation_uses_latest_successful_vercel_preview() -> None:
    client = FakePreviewClient(
        statuses=[
            {"state": "success", "target_url": "https://old-preview.vercel.app", "updated_at": "2026-06-20T01:00:00Z"},
            {"state": "failure", "target_url": "https://failed-preview.vercel.app", "updated_at": "2026-06-20T03:00:00Z"},
        ],
        checks=[
            {"status": "completed", "conclusion": "success", "details_url": "https://new-preview.vercel.app", "completed_at": "2026-06-20T04:00:00Z"}
        ],
    )

    request = anyio.run(_build_request, client)

    assert request.target_url == "https://new-preview.vercel.app"
    assert request.target_url_source == "github_commit_preview_url"
    assert request.target_url != "https://apple.com"


def test_missing_pr_preview_stays_pending_and_does_not_use_default_target(monkeypatch) -> None:
    request = anyio.run(_build_request, FakePreviewClient())

    assert request.target_url is None
    assert request.target_url_source == "vercel_preview_pending"
    assert request.target_url_pending_reason

    monkeypatch.setattr(runtime_validation_store, "_hermes_client_factory", lambda: FailingHermesClient())
    result = anyio.run(_trigger_validation, request)

    assert result.status == "pending"
    assert result.hermes.target_url is None
    assert result.hermes.target_source == "vercel_preview_pending"
    assert result.error == request.target_url_pending_reason


def test_runtime_payload_includes_dispatchable_bb2_review_context() -> None:
    request = RuntimeValidationRequest(
        repo="marcus937/jarvis-codex-worker",
        issue_number=12,
        pr_number=101,
        branch="codex-m2/workflow-wf-123",
        base_branch="agent-integration",
        target_url="https://preview.example.vercel.app",
        target_url_source="github_commit_preview_url",
        validation_type="python",
        requested_by="codex-m2",
        correlation_id="agtask-123",
        work_item_id="work-123",
        evidence_id="evidence-123",
        review_agent="bb2",
        workflow_id="wf-123",
        review_dispatch={
            "prompt": "Review Codex worker implementation evidence.",
            "changed_files": ["docs/example.md"],
            "commit_sha": "abc123",
        },
    )

    payload = _build_runtime_payload(
        request,
        "https://preview.example.vercel.app",
        "agtask-123",
        _settings(),
        target_source="github_commit_preview_url",
    )

    assert payload["work_item_id"] == "work-123"
    assert payload["evidence_id"] == "evidence-123"
    assert payload["review_agent"] == "bb2"
    assert payload["workflow_id"] == "wf-123"
    assert payload["payload"]["work_item_id"] == "work-123"
    assert payload["payload"]["evidence_id"] == "evidence-123"

    review_dispatch = payload["payload"]["review_dispatch"]
    assert payload["review_dispatch"] == review_dispatch
    assert review_dispatch["repository"] == "marcus937/jarvis-codex-worker"
    assert review_dispatch["repo"] == "marcus937/jarvis-codex-worker"
    assert review_dispatch["title"] == "BB2 review for marcus937/jarvis-codex-worker PR #101"
    assert review_dispatch["prompt"] == "Review Codex worker implementation evidence."
    assert review_dispatch["owner_agent"] == "bb2"
    assert review_dispatch["review_agent"] == "bb2"
    assert review_dispatch["target_agent"] == "bb2"
    assert review_dispatch["pr_number"] == 101
    assert review_dispatch["branch"] == "codex-m2/workflow-wf-123"
    assert review_dispatch["base_branch"] == "agent-integration"
    assert review_dispatch["work_item_id"] == "work-123"
    assert review_dispatch["evidence_id"] == "evidence-123"
    assert review_dispatch["evidence_packet_id"] == "evidence-123"
    assert review_dispatch["correlation_id"] == "agtask-123"
    assert review_dispatch["workflow_id"] == "wf-123"
    assert review_dispatch["source"] == "riseos-agent-orchestrator"
    assert review_dispatch["changed_files"] == ["docs/example.md"]
    assert review_dispatch["commit_sha"] == "abc123"
    assert "dispatch_prompt" in review_dispatch["tool_preference"]
    assert "create_review_packet" in review_dispatch["tool_preference"]