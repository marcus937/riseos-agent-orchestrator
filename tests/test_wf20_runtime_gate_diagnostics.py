import json
from typing import Any

import pytest

from app.config import Settings
from app.github_events import ParsedGitHubEvent, parse_github_event
from app.main import _log_wf20_runtime_gate_decision
from app.wf20_deployment_resume import is_wf20_deployment_status_payload
from app.wf20_runtime_validation import runtime_validation_required_for_parsed


def _pull_request_payload(
    *,
    action: str,
    repo: str = "marcus937/jarvis-mission-control",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": repo},
        "number": 42,
        "pull_request": {
            "number": 42,
            "head": {
                "sha": "abc123def456",
                "ref": "circuit/wf20-diagnostics",
                "repo": {"full_name": repo},
            },
            "base": {
                "ref": "agent-integration",
                "repo": {"full_name": repo},
            },
            "labels": [{"name": label} for label in labels or []],
        },
    }


def _issues_payload(*, repo: str = "marcus937/jarvis-mission-control") -> dict[str, Any]:
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
    assert event["repository"] == "marcus937/jarvis-mission-control"
    assert event["pull_request_number"] == 42
    assert event["head_sha"] == "abc123def456"
    assert event["head_ref"] == "circuit/wf20-diagnostics"
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
