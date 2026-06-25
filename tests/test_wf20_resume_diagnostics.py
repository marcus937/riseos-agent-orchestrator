from __future__ import annotations

from typing import Any

from app.circuit_runtime_validation import RuntimeValidationRequest
from app.github_events import parse_github_event
from app import wf20_resume_diagnostics as diagnostics


def request() -> RuntimeValidationRequest:
    item = RuntimeValidationRequest(
        repo="marcus937/jarvis-mission-control",
        pr_number=149,
        branch="codex-m2/wf20",
        target_url="https://preview.vercel.app",
        requested_by="orchestrator_wf20",
        workflow_id="wf20-marcus937-jarvis-mission-control-pr-149-abc123",
        correlation_id="wf20-marcus937-jarvis-mission-control-pr-149-abc123",
    )
    object.__setattr__(item, "commit_sha", "abc123")
    return item


def deployment_payload(state: str = "success") -> dict[str, Any]:
    return {
        "action": state,
        "repository": {"full_name": "marcus937/jarvis-mission-control"},
        "deployment": {"id": 9001, "sha": "abc123", "ref": "codex-m2/wf20", "environment": "Preview"},
        "deployment_status": {
            "id": 42,
            "state": state,
            "environment": "Preview",
            "target_url": "https://preview.vercel.app",
            "created_at": "2026-06-25T03:00:00Z",
        },
    }


def capture(monkeypatch: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def fake_log_event(event: str, **fields: Any) -> None:
        events.append({"event": event, **fields})

    monkeypatch.setattr(diagnostics, "log_event", fake_log_event)
    return events


def test_waiting_workflow_logs_required_fields(monkeypatch: Any) -> None:
    events = capture(monkeypatch)

    diagnostics.log_waiting_for_deployment(
        request(),
        reason="No verified Preview deployment yet.",
        runtime_validation_id="rv-1",
        pending_store_size_after_insert=3,
    )

    event = events[0]
    assert event["event"] == "WAITING_FOR_DEPLOYMENT"
    assert event["workflow_id"] == "wf20-marcus937-jarvis-mission-control-pr-149-abc123"
    assert event["repository"] == "marcus937/jarvis-mission-control"
    assert event["branch"] == "codex-m2/wf20"
    assert event["commit_sha"] == "abc123"
    assert event["pull_request"] == 149
    assert event["owner"] == "orchestrator_wf20"
    assert event["reason"] == "No verified Preview deployment yet."
    assert event["preview_url"] == "https://preview.vercel.app"
    assert event["runtime_validation_id"] == "rv-1"
    assert event["pending_store_size_after_insert"] == 3
    assert event["created_at"]


def test_deployment_webhook_logs_required_fields(monkeypatch: Any) -> None:
    events = capture(monkeypatch)
    parsed = parse_github_event("deployment_status", deployment_payload())

    diagnostics.log_deployment_status_received(parsed)

    event = events[0]
    assert event["event"] == "DEPLOYMENT_STATUS_RECEIVED"
    assert event["deployment_id"] == 9001
    assert event["deployment_status_id"] == 42
    assert event["repository"] == "marcus937/jarvis-mission-control"
    assert event["branch"] == "codex-m2/wf20"
    assert event["sha"] == "abc123"
    assert event["environment"] == "Preview"
    assert event["state"] == "success"
    assert event["target_url"] == "https://preview.vercel.app"
    assert event["created_at"] == "2026-06-25T03:00:00Z"


def test_failed_correlations_emit_rejection_reasons(monkeypatch: Any) -> None:
    events = capture(monkeypatch)

    diagnostics.log_correlation_candidates(
        waiting_workflows=[
            {"workflow_id": "wf-1", "commit_sha": "abc123", "branch": "codex-m2/a", "pull_request": 149},
            {"workflow_id": "wf-2", "commit_sha": "def456", "branch": "codex-m2/b", "pull_request": 150},
        ]
    )
    diagnostics.log_correlation_rejection(diagnostics.REJECTION_REASON_SHA_MISMATCH, workflow_id="wf-1")
    diagnostics.log_correlation_rejection(diagnostics.REJECTION_REASON_BRANCH_MISMATCH, workflow_id="wf-2")
    diagnostics.log_correlation_rejection(diagnostics.REJECTION_REASON_NO_RUNTIME_ITEM, workflow_id="wf-3")

    assert events[0]["event"] == "WF20_DEPLOYMENT_CORRELATION_CANDIDATES"
    assert events[0]["waiting_store_size"] == 2
    assert events[0]["candidate_workflow_ids"] == ["wf-1", "wf-2"]
    assert [event["rejection_reason"] for event in events[1:]] == [
        "SHA_MISMATCH",
        "BRANCH_MISMATCH",
        "NO_RUNTIME_ITEM",
    ]


def test_successful_correlation_emits_matched_workflow(monkeypatch: Any) -> None:
    events = capture(monkeypatch)

    diagnostics.log_matched_workflow(
        workflow_id="wf-1",
        correlation_method="SHA",
        selected_preview_url="https://preview.vercel.app",
        deployment_id=9001,
        deployment_status_id=42,
    )

    assert events[0] == {
        "event": "MATCHED_WORKFLOW",
        "workflow_id": "wf-1",
        "correlation_method": "SHA",
        "selected_preview_url": "https://preview.vercel.app",
        "deployment_id": 9001,
        "deployment_status_id": 42,
    }


def test_hermes_dispatch_emits_starting_hermes(monkeypatch: Any) -> None:
    events = capture(monkeypatch)

    diagnostics.log_starting_hermes(request(), runtime_validation_id="rv-1")

    assert events[0]["event"] == "STARTING_HERMES"
    assert events[0]["workflow_id"] == "wf20-marcus937-jarvis-mission-control-pr-149-abc123"
    assert events[0]["verified_preview_url"] == "https://preview.vercel.app"
    assert events[0]["runtime_validation_id"] == "rv-1"


def test_no_match_path_emits_terminal_reason(monkeypatch: Any) -> None:
    events = capture(monkeypatch)

    diagnostics.log_hermes_not_launched(diagnostics.TERMINAL_NO_MATCH, request(), reason="No waiting workflow matched.")

    assert events[0]["event"] == "NO_MATCHING_WAITING_WORKFLOW"
    assert events[0]["workflow_id"] == "wf20-marcus937-jarvis-mission-control-pr-149-abc123"
    assert events[0]["reason"] == "No waiting workflow matched."
