from __future__ import annotations

import json
from datetime import UTC, datetime

from app.config import Settings
from app.github_events import GitHubEventType
from app.runtime_validation_trace import REJECTION_MESSAGE, TRACE_EVENT
from app.runtime_validation_trace_patch import install_runtime_validation_trace_patch
from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationHermesSummary,
    RuntimeValidationResult,
    RuntimeValidationStore,
)
from app.review_queue import ReviewWorkItem, ReviewWorkItemStatus, _blocked_reason, review_queue
from app.runtime_validation_review_bridge import enqueue_review_from_runtime_validation


def _trace_events(caplog) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for record in caplog.records:
        try:
            payload = json.loads(record.getMessage())
        except json.JSONDecodeError:
            continue
        if payload.get("event") == TRACE_EVENT:
            events.append(payload)
    return events


def _runtime_result() -> RuntimeValidationResult:
    now = datetime.now(UTC)
    return RuntimeValidationResult(
        validation_id="rv-123",
        status="completed",
        repo="marcus937/jarvis-mission-control",
        pr_number=38,
        branch="circuit/frontend-change",
        base_branch="agent-integration",
        work_item_id="wi-123",
        evidence_id="ev-123",
        workflow_id="wf-123",
        review_dispatch={"commit_sha": "abc123", "evidence_packet_id": "ev-123"},
        validation_type="playwright",
        requested_by="orchestrator_wf20",
        created_at=now,
        completed_at=now,
        correlation_id="wf-123",
        hermes=RuntimeValidationHermesSummary(job_id="job-123", target_url="https://preview.vercel.app", status="PASSED"),
        evidence=RuntimeValidationEvidenceSummary(
            final_url="https://preview.vercel.app",
            http_status=200,
            screenshot_present=True,
            artifacts=[{"file_name": "summary.json", "sha256": "abc123"}],
        ),
        bb2=RuntimeValidationBB2Packet(packet_created=True, review_requested=False, review_status="approved"),
    )


def test_runtime_validation_store_get_logs_lookup_result(caplog) -> None:
    install_runtime_validation_trace_patch()
    caplog.set_level("INFO", logger="riseos_agent_orchestrator")
    result = _runtime_result()
    store = RuntimeValidationStore()
    store._items[result.validation_id] = result

    assert store.get("rv-123") is result
    assert store.get("missing-rv") is None

    events = _trace_events(caplog)
    found = [event for event in events if event.get("stage") == "runtime_validation_store_get" and event.get("lookup_result") == "found"]
    missing = [event for event in events if event.get("stage") == "runtime_validation_store_get" and event.get("lookup_result") == "missing"]
    assert found
    assert found[0]["runtime_validation_id"] == "rv-123"
    assert found[0]["work_item_id"] == "wi-123"
    assert found[0]["workflow_id"] == "wf-123"
    assert found[0]["evidence_packet_id"] == "ev-123"
    assert found[0]["repository"] == "marcus937/jarvis-mission-control"
    assert found[0]["commit_sha"] == "abc123"
    assert found[0]["pr_number"] == 38
    assert missing
    assert missing[0]["missing_field"] == "runtime_validation_id"


def test_review_bridge_logs_lookup_and_context_attachment(caplog) -> None:
    install_runtime_validation_trace_patch()
    review_queue.reset()
    caplog.set_level("INFO", logger="riseos_agent_orchestrator")
    result = _runtime_result()
    settings = Settings(enable_runtime_validation_review_bridge=True)

    item = enqueue_review_from_runtime_validation(result, settings)

    assert item is not None
    assert item.runtime_validation_id == "rv-123"
    assert item.runtime_validation_context["validation_id"] == "rv-123"
    events = _trace_events(caplog)
    stages = {str(event.get("stage")) for event in events}
    assert "runtime_validation_review_bridge_enqueue" in stages
    assert "review_gate_exact_runtime_result_lookup" in stages
    assert "review_gate_pending_work_item_lookup" in stages
    assert "runtime_validation_context_attachment_completed" in stages
    attached = [event for event in events if event.get("stage") == "runtime_validation_context_attachment_completed"]
    assert attached[0]["runtime_validation_id"] == "rv-123"
    assert attached[0]["work_item_id"] == item.id
    assert attached[0]["repository"] == "marcus937/jarvis-mission-control"
    assert attached[0]["pr_number"] == 38


def test_review_gate_logs_missing_runtime_evidence_without_changing_decision(caplog) -> None:
    install_runtime_validation_trace_patch()
    caplog.set_level("INFO", logger="riseos_agent_orchestrator")
    item = ReviewWorkItem(
        id="review-item-123",
        created_at=datetime.now(UTC),
        repo_full_name="marcus937/jarvis-mission-control",
        event_type=GitHubEventType.PULL_REQUEST,
        branch="circuit/frontend-change",
        commit_sha="abc123",
        pr_number=38,
        status=ReviewWorkItemStatus.PENDING_REVIEW,
    )

    reason = _blocked_reason(item)

    assert reason is None
    events = _trace_events(caplog)
    missing = [event for event in events if event.get("stage") == "review_gate_required_evidence_missing"]
    assert missing
    assert missing[0]["runtime_validation_id"] is None
    assert missing[0]["work_item_id"] == "review-item-123"
    assert missing[0]["repository"] == "marcus937/jarvis-mission-control"
    assert missing[0]["commit_sha"] == "abc123"
    assert missing[0]["pr_number"] == 38
    assert missing[0]["lookup_key"] == "ReviewWorkItem.runtime_validation_context"
    assert missing[0]["lookup_result"] == "missing"
    assert missing[0]["missing_field"] == "runtime_validation_context"
    assert missing[0]["rejection_reason"] == REJECTION_MESSAGE
