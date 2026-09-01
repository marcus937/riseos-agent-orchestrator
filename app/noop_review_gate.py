from __future__ import annotations

import logging
from typing import Any

from app.agent_tasks import AgentTask, AgentTaskExecutionResult, AgentTaskStatus, AgentTaskStore
from app.clients.agent_bus import AgentBusAPIError, AgentBusClient
from app.review_dispatch import reconcile_bb2_review_request_status

logger = logging.getLogger(__name__)


def _has_safe_pull_request_reference(evidence: dict[str, Any]) -> bool:
    pr_number = evidence.get("pr_number")
    pull_request = evidence.get("pull_request")
    if pr_number is None and pull_request is None:
        return True
    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
        return False
    if not isinstance(pull_request, dict):
        return False
    referenced_number = pull_request.get("number")
    return (
        pull_request.get("status") == "existing"
        and referenced_number == pr_number
    )


def is_verified_noop_execution(payload: AgentTaskExecutionResult) -> bool:
    """Return true only for a successful execution that provably changed nothing."""

    evidence = payload.evidence if isinstance(payload.evidence, dict) else {}
    review_dispatch = evidence.get("review_dispatch")
    if not isinstance(review_dispatch, dict):
        return False
    evidence_packet_id = review_dispatch.get("evidence_packet_id") or review_dispatch.get("evidence_id")
    return all(
        (
            payload.status == AgentTaskStatus.COMPLETED,
            payload.commit_sha is None,
            not payload.changed_files,
            evidence.get("execution_type") == "no_op",
            evidence.get("no_op") is True,
            evidence.get("success") is True,
            evidence.get("codex_exit_code") == 0,
            evidence.get("codex_timed_out") is False,
            evidence.get("push_success") is False,
            _has_safe_pull_request_reference(evidence),
            isinstance(evidence_packet_id, str) and bool(evidence_packet_id.strip()),
        )
    )


async def finalize_verified_noop_review(
    task: AgentTask,
    payload: AgentTaskExecutionResult,
    client: AgentBusClient,
    *,
    reviewer: str,
    store: AgentTaskStore,
) -> bool:
    """Complete BB2's envelope for an evidence-backed no-op without a GitHub event."""

    if not is_verified_noop_execution(payload):
        return False
    evidence = task.execution_evidence if isinstance(task.execution_evidence, dict) else {}
    review_work_item_id = evidence.get("agent_bus_review_work_item_id")
    review_dispatch = payload.evidence.get("review_dispatch")
    evidence_packet_id = review_dispatch.get("evidence_packet_id") or review_dispatch.get("evidence_id")
    if not isinstance(review_work_item_id, str) or not review_work_item_id.strip():
        return False

    try:
        await client.claim_review_request(
            review_work_item_id,
            reviewer=reviewer,
            actor=reviewer,
        )
    except AgentBusAPIError as exc:
        if exc.status_code != 409:
            raise

    await client.submit_bb2_review(
        {
            "work_item_id": review_work_item_id,
            "agent_id": reviewer,
            "decision": "approved",
            "confidence": 1.0,
            "rationale": (
                "Verified no-op: Codex exited successfully and reported no changed files, "
                "commit, push, or pull request."
            ),
            "findings": [],
            "required_changes": [],
            "risk_level": "low",
            "evidence_packet_ids_reviewed": [evidence_packet_id],
            "files_reviewed": [],
            "verified": [
                "Codex exit code was zero.",
                "Execution was explicitly classified as no_op.",
                "No files, commit, push, or pull request were produced.",
            ],
            "metadata": {
                "agent_task_id": task.task_id,
                "implementation_work_item_id": task.agent_bus_work_item_id,
                "review_mode": "verified_noop",
            },
        }
    )
    await reconcile_bb2_review_request_status(task, client, store=store)
    logger.info(
        "Verified no-op BB2 envelope finalized task_id=%s review_work_item_id=%s",
        task.task_id,
        review_work_item_id,
    )
    return True
