from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationHermesSummary,
    RuntimeValidationResult,
)
from app.config import Settings
from app.runtime_validation_agent_bus_bridge import advance_agent_bus_from_runtime_validation


class FakeAgentBusLifecycleClient:
    def __init__(self, status: str = "IN_PROGRESS") -> None:
        self.status = status
        self.review_attached = False
        self.review_packets: list[dict[str, Any]] = []
        self.attachments: list[dict[str, Any]] = []
        self.transitions: list[str] = []
        self.completions: list[dict[str, Any]] = []
        self.fetched: list[str] = []

    async def get_work_item(self, work_item_id: str) -> dict[str, Any]:
        self.fetched.append(work_item_id)
        return {"id": work_item_id, "status": self.status}

    async def create_review_packet(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert isinstance(payload.get("test_results"), dict)
        assert all(isinstance(artifact, str) for artifact in payload.get("artifacts", []))
        self.review_packets.append(payload)
        return {"id": f"review-packet-{len(self.review_packets)}"}

    async def attach_review_to_work_item(self, work_item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.review_attached = True
        self.attachments.append({"work_item_id": work_item_id, **payload})
        return {"attached": True, "work_item_id": work_item_id}

    async def transition_work_item(
        self,
        work_item_id: str,
        *,
        status: str,
        actor: str,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        owner_agent: str | None = None,
        review_agent: str | None = None,
    ) -> dict[str, Any]:
        expected = {
            "QUEUED": "CLAIMED",
            "CLAIMED": "IN_PROGRESS",
            "IN_PROGRESS": "READY_FOR_REVIEW",
            "AWAITING_EVIDENCE": "READY_FOR_REVIEW",
            "READY_FOR_REVIEW": "REVIEW_IN_PROGRESS",
            "REVIEW_IN_PROGRESS": "APPROVED",
        }
        assert expected[self.status] == status
        if status == "APPROVED":
            assert self.review_attached is True
        self.status = status
        self.transitions.append(status)
        return {"id": work_item_id, "status": self.status}

    async def complete_work_item(
        self,
        work_item_id: str,
        *,
        actor: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert self.status == "APPROVED"
        self.status = "COMPLETED"
        self.completions.append({"work_item_id": work_item_id, "actor": actor, "metadata": metadata})
        return {"id": work_item_id, "status": self.status}


def _settings() -> Settings:
    return Settings(
        enable_agent_bus_dispatch=True,
        agent_bus_base_url="https://agent-bus.example.test",
        agent_bus_token="agent-bus-token",
        agent_bus_runtime_validation_token="runtime-token",
    )


def _result(
    *,
    hermes_status: str = "PASSED",
    work_item_id: str | None = "work-item-123",
    artifacts: list[dict[str, Any]] | None = None,
) -> RuntimeValidationResult:
    review_status = "approved" if hermes_status == "PASSED" else "needs_changes"
    return RuntimeValidationResult(
        validation_id="runtime-validation-123",
        status="completed",
        repo="marcus937/jarvis-mission-control",
        issue_number=43,
        pr_number=38,
        branch="codex-m2/frontend-change",
        base_branch="agent-integration",
        work_item_id=work_item_id,
        evidence_id="evidence-123",
        review_agent="bb2",
        workflow_id="workflow-123",
        review_dispatch={"commit_sha": "abc123", "execution_type": "frontend"},
        validation_type="playwright",
        requested_by="circuit",
        created_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        correlation_id="correlation-123",
        hermes=RuntimeValidationHermesSummary(
            job_id="hermes-job-123",
            target_url="https://jarvis-mission-control-gules.vercel.app",
            target_source="vercel_deployment_status",
            status=hermes_status,
            manifest_fetched=True,
            bundle_fetched=True,
        ),
        evidence=RuntimeValidationEvidenceSummary(
            page_title="Jarvis Mission Control",
            final_url="https://jarvis-mission-control-gules.vercel.app",
            http_status=200,
            screenshot_present=True,
            artifacts=artifacts or [{"file_name": "summary.json", "sha256": "abc123"}],
        ),
        bb2=RuntimeValidationBB2Packet(packet_created=True, review_status=review_status),
    )


def test_passed_runtime_validation_advances_original_agent_bus_item_to_completed() -> None:
    client = FakeAgentBusLifecycleClient(status="IN_PROGRESS")

    response = asyncio.run(advance_agent_bus_from_runtime_validation(_result(), _settings(), agent_bus_client=client))

    assert response is not None
    assert response["status"] == "COMPLETED"
    assert client.status == "COMPLETED"
    assert client.review_packets[0]["reviewer"] == "bb2"
    assert client.review_packets[0]["review_status"] == "approved"
    assert client.review_packets[0]["metadata"]["approval_scope"] == "agent_bus_workflow_progression_only"
    assert client.review_packets[0]["metadata"]["merge_authorization"] is False
    assert client.attachments[0]["review_packet_id"] == "review-packet-1"
    assert client.transitions == ["READY_FOR_REVIEW", "REVIEW_IN_PROGRESS", "APPROVED"]
    assert len(client.completions) == 1


def test_passed_runtime_validation_review_packet_matches_agent_bus_schema() -> None:
    client = FakeAgentBusLifecycleClient(status="IN_PROGRESS")

    asyncio.run(advance_agent_bus_from_runtime_validation(_result(), _settings(), agent_bus_client=client))

    packet = client.review_packets[0]
    assert isinstance(packet["test_results"], dict)
    assert packet["test_results"]["hermes_playwright_runtime_validation"]["status"] == "PASSED"
    assert packet["artifacts"] == ["summary.json"]
    assert packet["metadata"]["artifact_metadata"] == [{"file_name": "summary.json", "sha256": "abc123"}]
    assert packet["metadata"]["approval_scope"] == "agent_bus_workflow_progression_only"
    assert packet["metadata"]["merge_authorization"] is False


def test_hermes_artifact_dicts_are_converted_to_string_references() -> None:
    client = FakeAgentBusLifecycleClient(status="IN_PROGRESS")
    artifacts = [
        {"retrieval": "GET /api/v1/evidence/hermes-job-123/files/summary.json", "file_name": "summary.json"},
        {"url": "https://agent-bus.example.test/evidence/screenshot.png", "file_name": "screenshot.png"},
        {"sha256": "stable-sha-only"},
    ]

    asyncio.run(
        advance_agent_bus_from_runtime_validation(_result(artifacts=artifacts), _settings(), agent_bus_client=client)
    )

    assert client.review_packets[0]["artifacts"] == [
        "GET /api/v1/evidence/hermes-job-123/files/summary.json",
        "https://agent-bus.example.test/evidence/screenshot.png",
        "stable-sha-only",
    ]
    assert all(isinstance(artifact, str) for artifact in client.review_packets[0]["artifacts"])


def test_passed_runtime_validation_replay_is_idempotent_for_completed_work_item() -> None:
    client = FakeAgentBusLifecycleClient(status="COMPLETED")

    response = asyncio.run(advance_agent_bus_from_runtime_validation(_result(), _settings(), agent_bus_client=client))

    assert response == {"status": "COMPLETED", "skipped": True, "reason": "work_item_already_completed"}
    assert client.review_packets == []
    assert client.attachments == []
    assert client.transitions == []
    assert client.completions == []


def test_failed_runtime_validation_attaches_blocking_review_without_completing() -> None:
    client = FakeAgentBusLifecycleClient(status="IN_PROGRESS")

    response = asyncio.run(
        advance_agent_bus_from_runtime_validation(_result(hermes_status="FAILED"), _settings(), agent_bus_client=client)
    )

    assert response is not None
    assert response["reason"] == "runtime_validation_not_passed"
    assert client.status == "IN_PROGRESS"
    assert client.review_packets[0]["review_status"] == "needs_changes"
    assert client.attachments[0]["review_status"] == "needs_changes"
    assert client.transitions == []
    assert client.completions == []


def test_runtime_validation_without_work_item_id_preserves_internal_review_queue_behavior() -> None:
    client = FakeAgentBusLifecycleClient(status="IN_PROGRESS")

    response = asyncio.run(
        advance_agent_bus_from_runtime_validation(_result(work_item_id=None), _settings(), agent_bus_client=client)
    )

    assert response is None
    assert client.fetched == []
    assert client.review_packets == []
    assert client.attachments == []
    assert client.transitions == []
    assert client.completions == []
