from __future__ import annotations

import logging
from typing import Any

from app.agent_task_release import release_runnable_agent_tasks
from app.agent_tasks import (
    AgentTask,
    AgentTaskStatus,
    AgentTaskStore,
    build_agent_task_store,
)
from app.clients.agent_bus import AgentBusAPIError, AgentBusClient
from app.config import Settings
from app.review_dispatch import reconcile_bb2_review_request_status
from app.review_queue import ReviewProcessResponse

logger = logging.getLogger(__name__)

_APPROVED = "approved_for_human_review"
_CHANGES_REQUESTED = {"needs_changes", "blocked", "escalate_to_marcus"}


async def finalize_review_gated_agent_task(
    response: ReviewProcessResponse,
    settings: Settings,
    *,
    store: AgentTaskStore | None = None,
    agent_bus_client: AgentBusClient | None = None,
    dependency_client: object | None = None,
) -> bool:
    """Apply a completed Hermes/BB2 PR review to its pending Agent Task gate."""

    decision = response.decision.decision.value.lower()
    if decision != _APPROVED and decision not in _CHANGES_REQUESTED:
        return False

    task_store = store or build_agent_task_store(settings.orchestrator_db_path)
    task = _matching_review_gated_task(
        task_store.list_agent_tasks(),
        repo_full_name=response.work_item.repo_full_name,
        branch=response.work_item.branch,
        commit_sha=response.work_item.commit_sha,
    )
    if task is None:
        return False

    evidence = (
        task.execution_evidence if isinstance(task.execution_evidence, dict) else {}
    )
    review_work_item_id = evidence.get("agent_bus_review_work_item_id")
    evidence_packet_id = evidence.get("evidence_packet_id")
    if not isinstance(review_work_item_id, str) or not review_work_item_id.strip():
        logger.warning(
            "Review-gated Agent Task is missing its BB2 envelope task_id=%s",
            task.task_id,
        )
        return True

    client = agent_bus_client or AgentBusClient(
        base_url=settings.agent_bus_base_url,
        token=settings.agent_bus_token,
        timeout_seconds=settings.agent_bus_timeout_seconds,
    )
    owns_client = agent_bus_client is None
    try:
        await _claim_review_envelope(
            client, review_work_item_id, reviewer=settings.agent_bus_review_agent
        )
        await client.submit_bb2_review(
            _bb2_review_payload(
                response,
                review_work_item_id=review_work_item_id,
                evidence_packet_id=evidence_packet_id,
                reviewer=settings.agent_bus_review_agent,
            )
        )
        await reconcile_bb2_review_request_status(task, client, store=task_store)
        refreshed = task_store.get_agent_task(task.task_id) or task
        if refreshed.status == AgentTaskStatus.COMPLETED:
            await release_runnable_agent_tasks(
                task_store,
                client,
                review_agent=settings.agent_bus_review_agent,
                dependency_client=dependency_client,
                correlation_id=task.correlation_id,
                settings=settings,
            )
        return True
    finally:
        if owns_client:
            await client.aclose()


async def _claim_review_envelope(
    client: AgentBusClient, work_item_id: str, *, reviewer: str
) -> None:
    try:
        await client.claim_review_request(
            work_item_id, reviewer=reviewer, actor=reviewer
        )
    except AgentBusAPIError as exc:
        if exc.status_code != 409:
            raise


def _matching_review_gated_task(
    tasks: list[AgentTask],
    *,
    repo_full_name: str | None,
    branch: str | None,
    commit_sha: str | None,
) -> AgentTask | None:
    candidates = [
        task
        for task in tasks
        if task.status == AgentTaskStatus.READY_FOR_REVIEW
        and repo_full_name is not None
        and task.repo_full_name == repo_full_name
    ]
    if commit_sha:
        exact_commit = [task for task in candidates if task.commit_sha == commit_sha]
        if exact_commit:
            return max(exact_commit, key=lambda task: task.updated_at)
    if branch:
        exact_branch = [task for task in candidates if task.branch == branch]
        if exact_branch:
            return max(exact_branch, key=lambda task: task.updated_at)
    return None


def _bb2_review_payload(
    response: ReviewProcessResponse,
    *,
    review_work_item_id: str,
    evidence_packet_id: Any,
    reviewer: str,
) -> dict[str, Any]:
    decision = (
        "approved" if response.decision.decision.value.lower() == _APPROVED else "needs_changes"
    )
    reviewed_ids = [str(evidence_packet_id)] if evidence_packet_id else []
    verified = [
        "Hermes runtime validation reached a terminal review decision.",
        "BB2 reviewed the GitHub diff and hydrated repository context.",
    ]
    if response.github_context_available:
        verified.append("GitHub changed-file and patch context was available.")
    return {
        "work_item_id": review_work_item_id,
        "agent_id": reviewer,
        "decision": decision,
        "confidence": response.decision.confidence,
        "rationale": response.decision.summary,
        "findings": list(response.decision.required_changes),
        "required_changes": list(response.decision.required_changes),
        "risk_level": response.decision.risk_level.value.lower(),
        "evidence_packet_ids_reviewed": reviewed_ids,
        "files_reviewed": list(response.changed_files),
        "verified": verified,
        "metadata": {
            "orchestrator_review_work_item_id": response.work_item.id,
            "repository": response.work_item.repo_full_name,
            "pr_number": response.work_item.pr_number,
            "commit_sha": response.work_item.commit_sha,
            "hermes_runtime_validation_id": response.work_item.runtime_validation_id,
            "hermes_runtime_validation_status": response.work_item.runtime_validation_status,
        },
    }
