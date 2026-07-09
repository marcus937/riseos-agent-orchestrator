from datetime import UTC, datetime

from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationHermesSummary,
    RuntimeValidationResult,
)
from app.config import Settings
from app.runtime_validation_review_bridge import enqueue_review_from_runtime_validation
from app.storage import SQLiteStateStore


def _settings() -> Settings:
    return Settings(
        github_webhook_secret="test-secret",
        orchestrator_admin_token="admin-token",
        enable_runtime_validation_review_bridge=True,
        hermes_m2_enable_dispatch=True,
        hermes_m2_base_url="https://hermes.example.test",
        hermes_m2_token="hermes-token",
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


def _result_with_nested_bb2_workflow_chain() -> RuntimeValidationResult:
    now = datetime.now(UTC)
    workflow_chain = _workflow_chain()
    return RuntimeValidationResult(
        validation_id="validation-wf21",
        status="completed",
        repo="marcus937/jarvis-mission-control",
        issue_number=None,
        pr_number=191,
        branch="codex-m2/wf21",
        base_branch="agent-integration",
        validation_type="playwright",
        requested_by="test",
        created_at=now,
        completed_at=now,
        correlation_id="wf-chain-21-29",
        work_item_id="agent-bus-wf21",
        workflow_id="wf-chain-21-29",
        review_dispatch={},
        hermes=RuntimeValidationHermesSummary(
            job_id="job-wf21",
            target_url="https://jarvis-mission-control-gules.vercel.app",
            target_source="vercel_preview_verified",
            status="PASSED",
            manifest_fetched=True,
            bundle_fetched=True,
        ),
        evidence=RuntimeValidationEvidenceSummary(
            page_title="Mission Control",
            final_url="https://jarvis-mission-control-gules.vercel.app",
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
            review_context={
                "workflow_id": "wf-chain-21-29",
                "work_item_id": "agent-bus-wf21",
                "review_dispatch": {
                    "workflow_chain": workflow_chain,
                    "workflow_chain_id": "wf-chain-21-29",
                    "workflow_step": "WF21",
                    "current_workflow_step": "WF21",
                    "next_workflow_step": "WF22",
                },
                "runtime_context": {
                    "workflow_chain": workflow_chain,
                    "workflow_chain_id": "wf-chain-21-29",
                    "current_workflow_step": "WF21",
                    "next_workflow_step": "WF22",
                },
            },
        ),
    )


def test_nested_runtime_validation_workflow_chain_is_persisted_and_reloaded(tmp_path) -> None:
    storage = SQLiteStateStore(str(tmp_path / "state.db"))
    result = _result_with_nested_bb2_workflow_chain()
    expected_chain = _workflow_chain()

    item = enqueue_review_from_runtime_validation(result, _settings(), storage=storage)

    assert item is not None
    assert item.runtime_validation_context["workflow_id"] == "wf-chain-21-29"
    assert item.runtime_validation_context["workflow_chain"] == expected_chain
    assert item.runtime_validation_context["review_dispatch"]["workflow_chain"] == expected_chain
    assert item.runtime_validation_context["metadata"]["workflow_chain"] == expected_chain
    assert len(item.runtime_validation_context["workflow_chain"]) > 0

    reloaded = storage.get_review_work_item(item.id)

    assert reloaded is not None
    assert reloaded.runtime_validation_context["workflow_id"] == "wf-chain-21-29"
    assert reloaded.runtime_validation_context["workflow_chain"] == expected_chain
    assert reloaded.runtime_validation_context["review_dispatch"]["workflow_chain"] == expected_chain
    assert reloaded.runtime_validation_context["workflow_chain"]["current_workflow_step"] == "WF21"
    assert reloaded.runtime_validation_context["workflow_chain"]["next_workflow_step"] == "WF22"
    assert len(reloaded.runtime_validation_context["workflow_chain"]) > 0
