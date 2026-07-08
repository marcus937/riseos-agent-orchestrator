from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationHermesSummary,
    RuntimeValidationRequest,
    RuntimeValidationResult,
)
from app.config import Settings
from app.github_events import GitHubEventType, parse_github_event
from app.reviewer.decision import ReviewDecisionType
from app.review_queue import ReviewWorkItem, ReviewWorkItemStatus
from app.runtime_validation_review_bridge import enqueue_review_from_runtime_validation
from app.storage import SQLiteStateStore
from app.task_dispatch import workflow_chain_continuation_for_decision
from app.wf20_deployment_resume import (
    claim_waiting_workflow_for_request,
    mark_waiting_workflow_resumed,
    persist_waiting_for_deployment,
)

REPO = "marcus937/jarvis-mission-control"
BRANCH = "codex-m2/wf21"
BASE_BRANCH = "agent-integration"
SHA = "abcdef1234567890abcdef1234567890abcdef12"
PR_NUMBER = 186
PREVIEW_URL = "https://jarvis-mission-control-pr-186.vercel.app"
WORKFLOW_ID = "wf20-chain-pr-186"
CORRELATION_ID = "wf20-chain-pr-186-runtime"
AGENT_BUS_WORK_ITEM_ID = "agent-bus-wf21"
REVIEW_ITEM_ID = "review-item-wf21"
RUNTIME_VALIDATION_ID = "runtime-validation-wf21"
WORKFLOW_STEPS = ["WF21", "WF22", "WF23", "WF24", "WF25", "WF26", "WF27", "WF28", "WF29"]


def test_wf20_resume_preserves_workflow_chain_until_continuation_selection(tmp_path: Any) -> None:
    storage = SQLiteStateStore(str(tmp_path / "orchestrator.db"))
    item = _waiting_review_item()

    waiting = persist_waiting_for_deployment(_waiting_request(), item, storage=storage)
    waiting_context = waiting.runtime_validation_context
    assert waiting_context["workflow_chain"]["workflow_step"] == "WF21"
    assert waiting_context["review_dispatch"]["workflow_step"] == "WF21"
    assert waiting_context["metadata"]["workflow_chain"]["next_workflow_step"] == "WF22"

    parsed = parse_github_event("deployment_status", _deployment_status_payload())
    claimed = claim_waiting_workflow_for_request(_ready_request(), parsed, storage=storage)
    assert claimed is not None
    assert claimed.id == REVIEW_ITEM_ID
    assert claimed.runtime_validation_context["workflow_chain"]["workflow_chain_id"] == WORKFLOW_ID

    resumed = mark_waiting_workflow_resumed(claimed, storage=storage)
    assert resumed.runtime_validation_context["workflow_chain"]["current_workflow_step"] == "WF21"

    review_item = enqueue_review_from_runtime_validation(
        _runtime_validation_result(),
        _settings(),
        storage=storage,
        existing_item=resumed,
    )
    assert review_item is not None

    reloaded = storage.get_review_work_item(review_item.id)
    assert reloaded is not None
    context = reloaded.runtime_validation_context
    review_dispatch = context["review_dispatch"]
    workflow_chain = context["workflow_chain"]

    assert workflow_chain["workflow_chain_id"] == WORKFLOW_ID
    assert workflow_chain["workflow_step"] == "WF21"
    assert workflow_chain["current_workflow_step"] == "WF21"
    assert workflow_chain["next_workflow_step"] == "WF22"
    assert workflow_chain["workflow_steps"] == WORKFLOW_STEPS
    assert review_dispatch["workflow_step"] == "WF21"
    assert review_dispatch["work_item_id"] == AGENT_BUS_WORK_ITEM_ID
    assert reloaded.repo_full_name == REPO
    assert reloaded.pr_number == PR_NUMBER
    assert reloaded.branch == BRANCH
    assert reloaded.base_branch == BASE_BRANCH
    assert reloaded.commit_sha == SHA
    assert reloaded.agent_bus_work_item_id == AGENT_BUS_WORK_ITEM_ID

    continuation = workflow_chain_continuation_for_decision(
        reloaded,
        ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW,
        base_branch=BASE_BRANCH,
    )
    assert continuation is not None
    assert continuation["workflow_chain_id"] == WORKFLOW_ID
    assert continuation["previous_workflow_step"] == "WF21"
    assert continuation["next_workflow_step"] == "WF22"
    assert continuation["following_workflow_step"] == "WF23"
    assert continuation["repository"] == REPO
    assert continuation["pr_number"] == PR_NUMBER
    assert continuation["branch"] == BRANCH
    assert continuation["base_branch"] == BASE_BRANCH
    assert continuation["commit_sha"] == SHA
    assert continuation["previous_work_item_id"] == AGENT_BUS_WORK_ITEM_ID


def _settings() -> Settings:
    return Settings(enable_runtime_validation_review_bridge=True)


def _workflow_chain() -> dict[str, Any]:
    return {
        "workflow_chain_id": WORKFLOW_ID,
        "workflow_family": "WF21-WF29",
        "workflow_step": "WF21",
        "current_workflow_step": "WF21",
        "next_workflow_step": "WF22",
        "final_workflow_step": "WF29",
        "workflow_steps": WORKFLOW_STEPS,
        "workflow_sequence": WORKFLOW_STEPS,
        "repository": REPO,
        "pr_number": PR_NUMBER,
        "branch": BRANCH,
        "base_branch": BASE_BRANCH,
        "work_item_id": AGENT_BUS_WORK_ITEM_ID,
        "previous_work_item_id": AGENT_BUS_WORK_ITEM_ID,
        "continuation_mode": "same_pr_branch",
        "merge_gate": "final_step_only",
    }


def _waiting_review_item() -> ReviewWorkItem:
    now = datetime.now(UTC)
    workflow_chain = _workflow_chain()
    return ReviewWorkItem(
        id=REVIEW_ITEM_ID,
        created_at=now,
        updated_at=now,
        repo_full_name=REPO,
        event_type=GitHubEventType.PULL_REQUEST,
        branch=BRANCH,
        base_branch=BASE_BRANCH,
        commit_sha=SHA,
        issue_number=PR_NUMBER,
        pr_number=PR_NUMBER,
        labels=["bb-review-needed", "runtime-agent"],
        status=ReviewWorkItemStatus.RUNTIME_VALIDATION_PENDING,
        agent_bus_work_item_id=AGENT_BUS_WORK_ITEM_ID,
        runtime_validation_id=RUNTIME_VALIDATION_ID,
        runtime_validation_context={
            "workflow_chain": workflow_chain,
            "metadata": {"workflow_chain": workflow_chain},
            "review_dispatch": {
                **workflow_chain,
                "repo": REPO,
                "work_item_id": AGENT_BUS_WORK_ITEM_ID,
                "previous_work_item_id": AGENT_BUS_WORK_ITEM_ID,
                "commit_sha": SHA,
            },
            "source": "pre_waiting_runtime_validation_context",
        },
    )


def _waiting_request() -> RuntimeValidationRequest:
    return RuntimeValidationRequest(
        repo=REPO,
        issue_number=PR_NUMBER,
        pr_number=PR_NUMBER,
        branch=BRANCH,
        base_branch=BASE_BRANCH,
        target_url=None,
        target_url_source="vercel_timeout",
        target_url_pending_reason="Waiting for Vercel Preview deployment.",
        requested_by="orchestrator_wf20",
        correlation_id=CORRELATION_ID,
        work_item_id=AGENT_BUS_WORK_ITEM_ID,
        workflow_id=WORKFLOW_ID,
        review_dispatch={
            **_workflow_chain(),
            "work_item_id": AGENT_BUS_WORK_ITEM_ID,
            "previous_work_item_id": AGENT_BUS_WORK_ITEM_ID,
            "commit_sha": SHA,
        },
    )


def _ready_request() -> RuntimeValidationRequest:
    return RuntimeValidationRequest(
        repo=REPO,
        issue_number=PR_NUMBER,
        pr_number=PR_NUMBER,
        branch=BRANCH,
        base_branch=BASE_BRANCH,
        target_url=PREVIEW_URL,
        target_url_source="github_verified_deployment_status_preview_url",
        requested_by="orchestrator_wf20",
        correlation_id=CORRELATION_ID,
        work_item_id=AGENT_BUS_WORK_ITEM_ID,
        workflow_id=WORKFLOW_ID,
        review_dispatch={
            **_workflow_chain(),
            "work_item_id": AGENT_BUS_WORK_ITEM_ID,
            "previous_work_item_id": AGENT_BUS_WORK_ITEM_ID,
            "commit_sha": SHA,
        },
    )


def _runtime_validation_result() -> RuntimeValidationResult:
    now = datetime.now(UTC)
    return RuntimeValidationResult(
        validation_id=RUNTIME_VALIDATION_ID,
        status="completed",
        repo=REPO,
        issue_number=PR_NUMBER,
        pr_number=PR_NUMBER,
        branch=BRANCH,
        base_branch=BASE_BRANCH,
        work_item_id=AGENT_BUS_WORK_ITEM_ID,
        evidence_id="evidence-wf21",
        review_agent="bb2",
        workflow_id=WORKFLOW_ID,
        review_dispatch={},
        validation_type="playwright",
        requested_by="orchestrator_wf20",
        created_at=now,
        completed_at=now,
        correlation_id=CORRELATION_ID,
        hermes=RuntimeValidationHermesSummary(
            job_id="hermes-job-wf21",
            target_url=PREVIEW_URL,
            target_source="github_verified_deployment_status_preview_url",
            status="PASSED",
        ),
        evidence=RuntimeValidationEvidenceSummary(screenshot_present=True),
        bb2=RuntimeValidationBB2Packet(packet_created=True, review_requested=True, review_status="approved"),
    )


def _deployment_status_payload() -> dict[str, Any]:
    return {
        "action": "success",
        "repository": {"full_name": REPO},
        "sender": {"login": "vercel"},
        "deployment": {"id": 186001, "ref": BRANCH, "sha": SHA, "environment": "Preview"},
        "deployment_status": {
            "id": 186002,
            "state": "success",
            "environment": "Preview",
            "target_url": PREVIEW_URL,
            "environment_url": PREVIEW_URL,
            "created_at": "2026-07-08T12:00:00Z",
        },
    }
