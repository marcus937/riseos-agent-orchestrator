from datetime import UTC, datetime

from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationHermesSummary,
    RuntimeValidationResult,
    _result_from_dispatch,
)
from app.config import Settings
from app.hermes_dispatch import HermesDispatchResult
from app.runtime_validation_review_bridge import enqueue_review_from_runtime_validation


def _settings() -> Settings:
    return Settings(
        github_webhook_secret="test-secret",
        orchestrator_admin_token="admin-token",
        enable_runtime_validation_review_bridge=True,
        hermes_m2_enable_dispatch=True,
        hermes_m2_base_url="https://hermes.example.test",
        hermes_m2_token="hermes-token",
        hermes_default_target="https://jarvis-mission-control-gules.vercel.app",
    )


def _workflow_chain() -> dict[str, object]:
    steps = ["WF21", "WF22", "WF23", "WF24", "WF25", "WF26", "WF27", "WF28", "WF29"]
    return {
        "workflow_chain_id": "wf-chain-21-29",
        "workflow_step": "WF21",
        "current_workflow_step": "WF21",
        "next_workflow_step": "WF22",
        "workflow_steps": steps,
        "workflow_sequence": steps,
        "repository": "marcus937/jarvis-mission-control",
        "pr_number": 191,
        "branch": "codex-m2/wf21",
        "base_branch": "agent-integration",
        "work_item_id": "agent-bus-wf21",
        "previous_work_item_id": "agent-bus-wf21",
        "continuation_mode": "same_pr_branch",
        "merge_gate": "final_step_only",
    }


def _pending_result_with_review_dispatch_chain() -> RuntimeValidationResult:
    now = datetime.now(UTC)
    workflow_chain = _workflow_chain()
    return RuntimeValidationResult(
        validation_id="validation-wf21",
        status="pending",
        repo="marcus937/jarvis-mission-control",
        issue_number=None,
        pr_number=191,
        branch="codex-m2/wf21",
        base_branch="agent-integration",
        work_item_id="agent-bus-wf21",
        evidence_id="evidence-wf21",
        review_agent="bb2",
        workflow_id="wf-chain-21-29",
        review_dispatch={
            "workflow_chain": workflow_chain,
            "workflow_chain_id": "wf-chain-21-29",
            "workflow_step": "WF21",
            "current_workflow_step": "WF21",
            "next_workflow_step": "WF22",
            "workflow_steps": workflow_chain["workflow_steps"],
            "workflow_sequence": workflow_chain["workflow_sequence"],
            "repository": "marcus937/jarvis-mission-control",
            "pr_number": 191,
            "branch": "codex-m2/wf21",
            "base_branch": "agent-integration",
            "work_item_id": "agent-bus-wf21",
        },
        validation_type="playwright",
        requested_by="test",
        created_at=now,
        correlation_id="wf-chain-21-29",
        hermes=RuntimeValidationHermesSummary(
            target_url="https://jarvis-mission-control-gules.vercel.app",
            target_source="vercel_preview_verified",
        ),
        evidence=RuntimeValidationEvidenceSummary(),
        bb2=RuntimeValidationBB2Packet(),
    )


def test_hermes_result_bb2_packet_carries_workflow_chain_into_initial_review_item() -> None:
    expected_chain = _workflow_chain()
    result = _pending_result_with_review_dispatch_chain()
    dispatch = HermesDispatchResult(
        attempted=True,
        success=True,
        status="PASSED",
        hermes_node="M2",
        correlation_id="wf-chain-21-29",
        target_url="https://jarvis-mission-control-gules.vercel.app",
        target_source="vercel_preview_verified",
        job_id="hermes-job-wf21",
    )

    completed = _result_from_dispatch(result, dispatch, _settings())

    assert completed.bb2.review_context["workflow_chain"] == expected_chain
    assert completed.bb2.review_context["runtime_context"]["workflow_chain"] == expected_chain
    assert completed.bb2.review_context["metadata"]["workflow_chain"] == expected_chain
    assert completed.bb2.review_context["current_workflow_step"] == "WF21"
    assert completed.bb2.review_context["next_workflow_step"] == "WF22"

    item = enqueue_review_from_runtime_validation(completed, _settings())

    assert item is not None
    context = item.runtime_validation_context
    assert context["workflow_id"] == "wf-chain-21-29"
    assert context["workflow_chain"] == expected_chain
    assert context["review_dispatch"]["workflow_chain"] == expected_chain
    assert context["metadata"]["workflow_chain"] == expected_chain
    assert context["workflow_chain"]["current_workflow_step"] == "WF21"
    assert context["workflow_chain"]["next_workflow_step"] == "WF22"
    assert len(context["workflow_chain"]["workflow_steps"]) == 9
    assert item.status.value == "pending_review"
