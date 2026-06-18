import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationHermesSummary,
    RuntimeValidationResult,
)
from app.config import Settings, get_settings
from app.event_store import event_store
from app.hermes_contract import runtime_validation_request_from_parsed
from app.main import app
from app.review_queue import ReviewLifecycleStage, ReviewWorkItemStatus, review_queue
from app.runtime_validation_review_bridge import enqueue_review_from_runtime_validation
from app.security import build_signature


@pytest.fixture(autouse=True)
def reset_state() -> None:
    get_settings.cache_clear()
    event_store.reset()
    review_queue.reset()
    app.dependency_overrides.clear()
    yield
    get_settings.cache_clear()
    app.dependency_overrides.clear()
    review_queue.reset()


def _settings(*, enabled: bool = True) -> Settings:
    return Settings(
        github_webhook_secret="test-secret",
        orchestrator_admin_token="admin-token",
        enable_runtime_validation_review_bridge=enabled,
        hermes_m2_enable_dispatch=True,
        hermes_m2_base_url="https://hermes.example.test",
        hermes_m2_token="hermes-token",
        hermes_default_target="https://jarvis-mission-control-gules.vercel.app",
    )


def _client(*, enabled: bool) -> TestClient:
    app.dependency_overrides[get_settings] = lambda: _settings(enabled=enabled)
    return TestClient(app)


def _signed_headers(event: str, payload: bytes) -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": build_signature("test-secret", payload),
        "Content-Type": "application/json",
    }


def _runtime_pr_payload(*, head_ref: str = "agent-integration", preview_url: str | None = None) -> dict[str, Any]:
    payload = {
        "action": "opened",
        "number": 111,
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "circuit"},
        "pull_request": {
            "number": 111,
            "merged": False,
            "head": {"sha": "abc123", "ref": head_ref, "repo": {"full_name": "riseos/example"}},
            "base": {"ref": "main", "repo": {"full_name": "riseos/example"}},
            "labels": [],
        },
    }
    if preview_url:
        payload["deployment_url"] = preview_url
    return payload


def _post_pr(client: TestClient, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    response = client.post("/webhooks/github", content=body, headers=_signed_headers("pull_request", body))
    assert response.status_code == 200


def _result(status: str = "completed", *, validation_id: str = "validation-1") -> RuntimeValidationResult:
    now = datetime.now(UTC)
    hermes_status = "FAILED" if status == "failed" else "PASSED"
    review_status = "needs_changes" if status == "failed" else "approved"
    error = "Playwright console errors found." if status == "failed" else None
    if status == "blocked":
        hermes_status = "BLOCKED"
        review_status = "blocked"
        error = "Hermes validation was blocked."
    return RuntimeValidationResult(
        validation_id=validation_id,
        status=status,
        repo="riseos/example",
        issue_number=None,
        pr_number=111,
        branch="agent-integration",
        validation_type="playwright",
        requested_by="test",
        created_at=now,
        completed_at=now if status != "pending" else None,
        correlation_id="runtime-validation-test",
        hermes=RuntimeValidationHermesSummary(
            job_id="job-111",
            target_url="https://jarvis-mission-control-gules.vercel.app",
            target_source="hermes_default_target",
            status=hermes_status,
            manifest_fetched=True,
            bundle_fetched=True,
            error=error,
        ),
        evidence=RuntimeValidationEvidenceSummary(
            page_title="Mission Control",
            final_url="https://jarvis-mission-control-gules.vercel.app",
            http_status=200,
            screenshot_present=True,
            console_error_count=1 if status == "failed" else 0,
            console_warning_count=0,
            network_failure_count=0,
            network_non_2xx_count=0,
            artifacts=[{"file_name": "summary.json", "size": 123, "sha256": "abc123"}],
        ),
        bb2=RuntimeValidationBB2Packet(packet_created=True, review_status=review_status, review_context={"source": "test"}),
        error=error,
    )


def test_successful_hermes_result_enqueues_bb2_review_with_evidence() -> None:
    result = _result("completed")

    item = enqueue_review_from_runtime_validation(result, _settings())

    assert item is not None
    assert item.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert item.lifecycle_stage == ReviewLifecycleStage.BB2_REVIEW_REQUESTED_FROM_RUNTIME_VALIDATION
    assert item.runtime_validation_context["validation_status"] == "completed"
    assert item.runtime_validation_context["screenshot_available"] is True
    assert item.runtime_validation_context["evidence_artifacts"][0]["sha256"] == "abc123"
    assert result.bb2.review_requested is True


def test_failed_hermes_result_enqueues_bb2_review_with_failure_context() -> None:
    item = enqueue_review_from_runtime_validation(_result("failed"), _settings())

    assert item is not None
    assert item.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert item.runtime_validation_status == "failed"
    assert item.runtime_validation_context["console_errors"] == 1
    assert item.runtime_validation_context["validation_status"] == "failed"


def test_blocked_hermes_result_enqueues_bb2_review_with_blocked_context() -> None:
    item = enqueue_review_from_runtime_validation(_result("blocked"), _settings())

    assert item is not None
    assert item.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert item.runtime_validation_status == "blocked"
    assert item.runtime_validation_context["hermes_status"] == "BLOCKED"
    assert item.runtime_validation_context["error"] == "Hermes validation was blocked."


def test_non_terminal_runtime_validation_does_not_enqueue_bb2_review() -> None:
    item = enqueue_review_from_runtime_validation(_result("pending"), _settings())

    assert item is None
    assert review_queue.list_items() == []


def test_duplicate_runtime_completion_is_idempotent_after_review_completed() -> None:
    result = _result("completed", validation_id="same-validation")
    first = enqueue_review_from_runtime_validation(result, _settings())
    assert first is not None
    first.status = ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW
    first.lifecycle_stage = ReviewLifecycleStage.REVIEW_COMPLETED

    second = enqueue_review_from_runtime_validation(result, _settings())

    assert first is second
    assert len(review_queue.list_items()) == 1
    assert second.lifecycle_stage == ReviewLifecycleStage.REVIEW_COMPLETED


def test_runtime_dependent_pr_is_not_scheduled_when_bridge_disabled() -> None:
    client = _client(enabled=False)
    _post_pr(client, _runtime_pr_payload())

    item = review_queue.list_items()[0]
    assert item.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert item.lifecycle_stage == ReviewLifecycleStage.REVIEW_QUEUED
    assert item.runtime_validation_context == {}


def test_runtime_dependent_pr_is_pending_before_terminal_validation(monkeypatch) -> None:
    client = _client(enabled=True)
    observed_pending = False

    async def fake_trigger(request: Any, settings: Any) -> RuntimeValidationResult:
        nonlocal observed_pending
        items = review_queue.list_items()
        observed_pending = len(items) == 1 and items[0].status == ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING
        return _result("completed")

    monkeypatch.setattr("app.main.runtime_validation_store.trigger", fake_trigger)
    _post_pr(client, _runtime_pr_payload())

    assert observed_pending is True
    item = review_queue.list_items()[0]
    assert item.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert item.lifecycle_stage == ReviewLifecycleStage.BB2_REVIEW_REQUESTED_FROM_RUNTIME_VALIDATION


def test_non_runtime_pr_flow_remains_unchanged() -> None:
    client = _client(enabled=True)
    _post_pr(client, _runtime_pr_payload(head_ref="feature/not-runtime"))

    item = review_queue.list_items()[0]
    assert item.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert item.lifecycle_stage == ReviewLifecycleStage.REVIEW_QUEUED
    assert item.runtime_validation_context == {}


def test_runtime_validation_request_preserves_payload_preview_url() -> None:
    from app.github_events import parse_github_event
    import anyio

    parsed = parse_github_event("pull_request", _runtime_pr_payload(preview_url="https://preview-riseos.vercel.app"))
    request = anyio.run(runtime_validation_request_from_parsed, parsed, _settings())

    assert request.target_url == "https://preview-riseos.vercel.app"
