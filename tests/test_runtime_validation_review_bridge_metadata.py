from datetime import UTC, datetime

from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationHermesSummary,
    RuntimeValidationResult,
)
from app.config import Settings
from app.review_queue import ReviewWorkItemStatus, review_queue
from app.runtime_validation_review_bridge import enqueue_review_from_runtime_validation


def setup_function() -> None:
    review_queue.reset()


def teardown_function() -> None:
    review_queue.reset()


def _settings() -> Settings:
    return Settings(
        github_webhook_secret="test-secret",
        orchestrator_admin_token="admin-token",
        enable_runtime_validation_review_bridge=True,
    )


def _result() -> RuntimeValidationResult:
    now = datetime.now(UTC)
    return RuntimeValidationResult(
        validation_id="validation-wf21",
        status="completed",
        repo="marcus937/jarvis-mission-control",
        issue_number=None,
        pr_number=42,
        branch="circuit/wf21-chain",
        base_branch="agent-integration",
        validation_type="playwright",
        requested_by="agent-bus",
        created_at=now,
        completed_at=now,
        workflow_id="workflow-chain-123",
        work_item_id="agent-bus-work-item-21",
        correlation_id="workflow-chain-123",
        review_dispatch={},
        hermes=RuntimeValidationHermesSummary(
            job_id="hermes-job-21",
            target_url="https://preview.example.test",
            target_source="vercel_preview",
            status="PASSED",
            manifest_fetched=True,
            bundle_fetched=True,
        ),
        evidence=RuntimeValidationEvidenceSummary(
            page_title="Mission Control",
            final_url="https://preview.example.test",
            http_status=200,
            screenshot_present=True,
            console_error_count=0,
            console_warning_count=0,
            network_failure_count=0,
            network_non_2xx_count=0,
            artifacts=[],
        ),
        bb2=RuntimeValidationBB2Packet(
            packet_created=True,
            review_status="approved",
            review_context={"source": "test"},
        ),
    )


def test_new_review_item_is_constructed_with_agent_bus_workflow_chain_metadata() -> None:
    workflow_sequence = ["WF21", "WF22", "WF23"]
    agent_bus_work_item = {
        "id": "agent-bus-work-item-21",
        "metadata": {
            "workflow_chain_id": "workflow-chain-123",
            "workflow_step": "WF21",
            "current_workflow_step": "WF21",
            "next_workflow_step": "WF22",
            "workflow_sequence": workflow_sequence,
            "workflow_chain": {
                "workflow_chain_id": "workflow-chain-123",
                "workflow_step": "WF21",
                "current_workflow_step": "WF21",
                "next_workflow_step": "WF22",
                "workflow_sequence": workflow_sequence,
                "continuation_mode": "same_pr_branch",
                "merge_gate": "final_step_only",
            },
            "repository": "marcus937/jarvis-mission-control",
            "pr_number": 42,
            "branch": "circuit/wf21-chain",
            "base_branch": "agent-integration",
        },
    }

    item = enqueue_review_from_runtime_validation(
        _result(),
        _settings(),
        agent_bus_work_item=agent_bus_work_item,
    )

    assert item is not None
    assert item.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert item.agent_bus_work_item_id == "agent-bus-work-item-21"
    assert item.runtime_validation_context["workflow_chain_id"] == "workflow-chain-123"
    assert item.runtime_validation_context["workflow_step"] == "WF21"
    assert item.runtime_validation_context["current_workflow_step"] == "WF21"
    assert item.runtime_validation_context["next_workflow_step"] == "WF22"
    assert item.runtime_validation_context["workflow_sequence"] == workflow_sequence

    review_dispatch = item.runtime_validation_context["review_dispatch"]
    assert review_dispatch["workflow_chain_id"] == "workflow-chain-123"
    assert review_dispatch["workflow_step"] == "WF21"
    assert review_dispatch["next_workflow_step"] == "WF22"
    assert review_dispatch["workflow_chain"]["workflow_sequence"] == workflow_sequence


def test_new_review_item_hydrates_workflow_chain_from_agent_bus_work_item_detail_envelope() -> None:
    workflow_sequence = [f"WF{step}" for step in range(21, 30)]
    workflow_chain = {
        "workflow_chain_id": "workflow-chain-123",
        "workflow_step": "WF21",
        "current_workflow_step": "WF21",
        "next_workflow_step": "WF22",
        "final_workflow_step": "WF29",
        "workflow_sequence": workflow_sequence,
        "workflow_steps": workflow_sequence,
        "continuation_mode": "same_pr_branch",
        "merge_gate": "final_step_only",
        "repository": "marcus937/jarvis-mission-control",
        "pr_number": 42,
        "branch": "circuit/wf21-chain",
        "base_branch": "agent-integration",
    }
    agent_bus_work_item_detail = {
        "work_item": {
            "work_item_id": "agent-bus-work-item-21",
            "metadata": {
                "workflow_chain": workflow_chain,
                "workflow_chain_id": "workflow-chain-123",
                "workflow_step": "WF21",
                "current_workflow_step": "WF21",
                "next_workflow_step": "WF22",
                "workflow_sequence": workflow_sequence,
                "workflow_steps": workflow_sequence,
                "repository": "marcus937/jarvis-mission-control",
                "pr_number": 42,
                "branch": "circuit/wf21-chain",
                "base_branch": "agent-integration",
            },
        },
        "history": [],
    }

    item = enqueue_review_from_runtime_validation(
        _result(),
        _settings(),
        agent_bus_work_item=agent_bus_work_item_detail,
    )

    assert item is not None
    assert item.status == ReviewWorkItemStatus.PENDING_REVIEW
    assert item.runtime_validation_context["workflow_chain_id"] == "workflow-chain-123"
    assert item.runtime_validation_context["workflow_step"] == "WF21"
    assert item.runtime_validation_context["current_workflow_step"] == "WF21"
    assert item.runtime_validation_context["next_workflow_step"] == "WF22"
    assert item.runtime_validation_context["workflow_sequence"] == workflow_sequence
    assert item.runtime_validation_context["workflow_steps"] == workflow_sequence

    hydrated_chain = item.runtime_validation_context["workflow_chain"]
    assert hydrated_chain["workflow_chain_id"] == "workflow-chain-123"
    assert hydrated_chain["current_workflow_step"] == "WF21"
    assert hydrated_chain["next_workflow_step"] == "WF22"
    assert len(hydrated_chain["workflow_sequence"]) == 9

    review_dispatch = item.runtime_validation_context["review_dispatch"]
    assert review_dispatch["workflow_chain"]["workflow_step"] == "WF21"
    assert review_dispatch["workflow_chain"]["next_workflow_step"] == "WF22"
