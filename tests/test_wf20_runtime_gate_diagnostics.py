import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.circuit_runtime_validation import RuntimeValidationRequest
from app.config import Settings, get_settings
from app.event_store import event_store
from app.github_events import ParsedGitHubEvent, parse_github_event
from app.main import _log_wf20_runtime_gate_decision, app
from app.review_queue import review_queue
from app.security import build_signature
from app.wf20_deployment_resume import is_wf20_deployment_status_payload
from app.wf20_runtime_validation import runtime_validation_required_for_parsed


REPO = "marcus937/jarvis-mission-control"
BRANCH = "circuit/wf20-diagnostics"
SHA = "abc123def456"
PREVIEW_URL = "https://jarvis-mission-control-git-circuit-wf20-diagnostics-marcus937.vercel.app"


def _pull_request_payload(
    *,
    action: str,
    repo: str = REPO,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": repo},
        "number": 42,
        "pull_request": {
            "number": 42,
            "head": {
                "sha": SHA,
                "ref": BRANCH,
                "repo": {"full_name": repo},
            },
            "base": {
                "ref": "agent-integration",
                "repo": {"full_name": repo},
            },
            "labels": [{"name": label} for label in labels or []],
        },
    }


def _deployment_status_payload(
    *,
    repo: str = REPO,
    branch: str = BRANCH,
    sha: str = SHA,
    state: str = "success",
    target_url: str | None = PREVIEW_URL,
) -> dict[str, Any]:
    deployment_status: dict[str, Any] = {
        "id": 222,
        "state": state,
        "environment": "Preview",
        "created_at": "2026-06-25T17:00:00Z",
    }
    if target_url:
        deployment_status["target_url"] = target_url
        deployment_status["environment_url"] = target_url
    return {
        "action": state,
        "repository": {"full_name": repo},
        "sender": {"login": "vercel"},
        "deployment": {"id": 111, "ref": branch, "sha": sha, "environment": "Preview"},
        "deployment_status": deployment_status,
    }


def _issues_payload(*, repo: str = REPO) -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"full_name": repo},
        "issue": {
            "number": 42,
            "title": "Frontend task",
            "state": "open",
            "labels": [],
        },
    }


def _event_records(caplog) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for record in caplog.records:
        try:
            events.append(json.loads(record.getMessage()))
        except json.JSONDecodeError:
            continue
    return events


def _emit_gate_decision(
    parsed: ParsedGitHubEvent,
    caplog,
    *,
    settings: Settings | None = None,
    has_review_context: bool = False,
) -> dict[str, Any]:
    caplog.set_level("INFO", logger="riseos_agent_orchestrator")
    settings = settings or Settings(enable_runtime_validation_review_bridge=True)
    runtime_gated = runtime_validation_required_for_parsed(parsed, settings, has_review_context=has_review_context)
    deployment_status_payload = is_wf20_deployment_status_payload(parsed)
    _log_wf20_runtime_gate_decision(
        parsed,
        settings,
        has_review_context=has_review_context,
        runtime_gated=runtime_gated,
        deployment_status_payload=deployment_status_payload,
    )
    return [event for event in _event_records(caplog) if event.get("event") == "wf20_runtime_gate_decision"][-1]


@pytest.mark.parametrize(
    ("parsed", "expected_reason"),
    [
        (parse_github_event("issues", _issues_payload()), "not_pull_request_event"),
        (parse_github_event("pull_request", _pull_request_payload(action="closed")), "unsupported_action"),
        (
            parse_github_event(
                "pull_request",
                _pull_request_payload(action="opened", repo="marcus937/backend-service"),
            ),
            "repo_profile_missing",
        ),
        (
            parse_github_event(
                "pull_request",
                _pull_request_payload(action="opened", labels=["documentation-only"]),
            ),
            "documentation_only_or_backend_only",
        ),
    ],
)
def test_wf20_runtime_gate_decision_logs_false_with_failed_gate_reason(parsed, expected_reason, caplog) -> None:
    event = _emit_gate_decision(parsed, caplog)

    assert event["runtime_gated"] is False
    assert event[expected_reason] is True
    assert event["enable_runtime_validation_review_bridge"] is True
    assert event["deployment_status_payload"] is False


def test_wf20_runtime_gate_decision_logs_false_when_bridge_disabled(caplog) -> None:
    parsed = parse_github_event("pull_request", _pull_request_payload(action="opened"))

    event = _emit_gate_decision(parsed, caplog, settings=Settings(enable_runtime_validation_review_bridge=False))

    assert event["runtime_gated"] is False
    assert event["bridge_disabled"] is True
    assert event["enable_runtime_validation_review_bridge"] is False


@pytest.mark.parametrize("action", ["opened", "synchronize", "ready_for_review"])
def test_wf20_runtime_gate_decision_logs_true_for_jmc_frontend_actions(action, caplog) -> None:
    parsed = parse_github_event("pull_request", _pull_request_payload(action=action))

    event = _emit_gate_decision(parsed, caplog)

    assert event["runtime_gated"] is True
    assert event["event_type"] == "pull_request"
    assert event["action"] == action
    assert event["repository"] == REPO
    assert event["pull_request_number"] == 42
    assert event["head_sha"] == SHA
    assert event["head_ref"] == BRANCH
    assert event["base_ref"] == "agent-integration"
    assert event["has_review_context"] is False
    assert event["deployment_status_payload"] is False
    assert event["validation_route_reason"] == f"pull_request_{action}_frontend_runtime_validation"
    assert event["frontend_validation_profile"] == {
        "requires_runtime_validation": True,
        "validation_profile": "jmc_frontend_preview_v1",
    }
    assert event["bridge_disabled"] is False
    assert event["not_pull_request_event"] is False
    assert event["unsupported_action"] is False
    assert event["repo_profile_missing"] is False
    assert event["documentation_only_or_backend_only"] is False


def test_ready_deployment_status_for_profiled_frontend_repo_is_runtime_gated(caplog) -> None:
    parsed = parse_github_event("deployment_status", _deployment_status_payload())

    event = _emit_gate_decision(parsed, caplog, has_review_context=False)

    assert is_wf20_deployment_status_payload(parsed) is True
    assert event["runtime_gated"] is True
    assert event["deployment_status_payload"] is True
    assert event["event_type"] == "pull_request"
    assert event["action"] == "ready_for_review"
    assert event["repository"] == REPO
    assert event["head_sha"] == SHA
    assert event["head_ref"] == BRANCH
    assert event["has_review_context"] is False
    assert event["frontend_validation_profile"] == {
        "requires_runtime_validation": True,
        "validation_profile": "jmc_frontend_preview_v1",
    }


def test_deployment_status_for_non_profiled_repo_logs_profile_gate_failure(caplog) -> None:
    parsed = parse_github_event(
        "deployment_status",
        _deployment_status_payload(repo="marcus937/backend-service", target_url="https://backend-service.vercel.app"),
    )

    event = _emit_gate_decision(parsed, caplog, has_review_context=False)

    assert event["runtime_gated"] is False
    assert event["deployment_status_payload"] is True
    assert event["repo_profile_missing"] is True
    assert event["frontend_validation_profile"] == {
        "requires_runtime_validation": False,
        "validation_profile": None,
    }


def test_ready_deployment_status_webhook_reaches_runtime_validation_request_builder(monkeypatch) -> None:
    get_settings.cache_clear()
    event_store.reset()
    review_queue.reset()
    app.dependency_overrides.clear()
    settings = Settings(
        github_webhook_secret="secret",
        enable_runtime_validation_review_bridge=True,
        enable_github_writeback=False,
        enable_agent_bus_dispatch=False,
        enable_task_dispatch=False,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    observed: list[ParsedGitHubEvent] = []

    async def fake_runtime_validation_request_from_parsed(
        parsed: ParsedGitHubEvent,
        settings: Settings,
        *,
        github_client: Any | None = None,
    ) -> RuntimeValidationRequest:
        observed.append(parsed)
        return RuntimeValidationRequest(
            repo=parsed.repository or "unknown",
            pr_number=parsed.pull_request_number,
            branch=parsed.head_ref,
            base_branch=parsed.base_ref,
            target_url=PREVIEW_URL,
            target_url_source="github_verified_deployment_status_preview_url",
            validation_type="playwright",
            requested_by="orchestrator_wf20",
            correlation_id="wf20-test-correlation",
            workflow_id="wf20-test-workflow",
        )

    monkeypatch.setattr("app.main.runtime_validation_request_from_parsed", fake_runtime_validation_request_from_parsed)
    client = TestClient(app)
    body = json.dumps(_deployment_status_payload()).encode()
    response = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "deployment_status",
            "X-GitHub-Delivery": "wf20-gate-delivery",
            "X-Hub-Signature-256": build_signature(settings.github_webhook_secret, body),
            "Content-Type": "application/json",
        },
    )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert observed
    assert observed[0].repository == REPO
    assert observed[0].action == "ready_for_review"
