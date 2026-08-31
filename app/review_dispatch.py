from __future__ import annotations

import json
import logging
from typing import Any

from app.agent_tasks import (
    AgentTask,
    AgentTaskExecutionResult,
    AgentTaskStatus,
    AgentTaskStore,
    mark_agent_task_review_approved,
    mark_agent_task_review_changes_requested,
)
from app.clients.agent_bus import AgentBusClient

logger = logging.getLogger(__name__)

REVIEW_WORK_ITEM_TYPE = "review_request"
REVIEW_QUEUE = "review"


async def dispatch_bb2_review_request_from_execution_result(
    task: AgentTask,
    payload: AgentTaskExecutionResult,
    client: AgentBusClient,
    *,
    review_agent: str,
    store: AgentTaskStore,
) -> str | None:
    """Create the canonical Agent Bus review request for a completed implementation task."""

    review_dispatch = _review_dispatch_from_payload(payload)
    if review_dispatch is None:
        logger.info("execution-result did not include review_dispatch; skipping Agent Bus review request task_id=%s", task.task_id)
        return None
    review_dispatch = _merge_workflow_chain_review_dispatch(task, review_dispatch)

    existing_id = task.execution_evidence.get("agent_bus_review_work_item_id")
    if isinstance(existing_id, str) and existing_id.strip():
        logger.info(
            "Agent Bus review request already exists task_id=%s review_work_item_id=%s",
            task.task_id,
            existing_id,
        )
        return existing_id

    review_request = build_agent_bus_review_request_payload(task, payload, review_dispatch, default_review_agent=review_agent)
    logger.info("agent_bus_review_dispatch_payload=%s", json.dumps(review_request, default=str))
    response = await client.create_work_item(review_request)
    review_work_item_id = response.get("work_item_id")
    if not isinstance(review_work_item_id, str) or not review_work_item_id.strip():
        raise RuntimeError("Agent Bus review request response did not include work_item_id.")

    task.execution_evidence = {
        **task.execution_evidence,
        "agent_bus_review_work_item_id": review_work_item_id,
        "bb2_review_request_status": "queued",
        "bb2_review_request_payload": review_request,
    }
    store.save_agent_task(task)
    logger.info(
        "BB2 review requested through Agent Bus task_id=%s work_item_id=%s review_work_item_id=%s reviewer=%s",
        task.task_id,
        task.agent_bus_work_item_id,
        review_work_item_id,
        review_request.get("review_agent"),
    )
    return review_work_item_id


async def reconcile_bb2_review_request_status(
    task: AgentTask,
    client: AgentBusClient,
    *,
    store: AgentTaskStore,
) -> AgentTask:
    """Refresh persisted BB2 request state from its canonical Agent Bus work item."""

    evidence = task.execution_evidence if isinstance(task.execution_evidence, dict) else {}
    review_work_item_id = evidence.get("agent_bus_review_work_item_id")
    if not isinstance(review_work_item_id, str) or not review_work_item_id.strip():
        return task

    try:
        response = await client.get_work_item(review_work_item_id)
    except Exception as exc:
        logger.warning(
            "Could not reconcile BB2 review request task_id=%s review_work_item_id=%s error=%s",
            task.task_id,
            review_work_item_id,
            exc,
        )
        return task

    work_item = response.get("work_item") if isinstance(response.get("work_item"), dict) else response
    status = work_item.get("status") if isinstance(work_item, dict) else None
    if not isinstance(status, str) or not status.strip():
        logger.warning(
            "Agent Bus BB2 review response did not include status task_id=%s review_work_item_id=%s",
            task.task_id,
            review_work_item_id,
        )
        return task

    metadata = work_item.get("metadata") if isinstance(work_item.get("metadata"), dict) else {}
    updated_evidence = {**evidence, "bb2_review_request_status": status.strip().lower()}
    for source_key, target_key in (
        ("source_review_decision", "bb2_review_decision"),
        ("source_review_packet_id", "bb2_review_packet_id"),
    ):
        value = metadata.get(source_key)
        if value is not None:
            updated_evidence[target_key] = value

    decision = str(updated_evidence.get("bb2_review_decision") or "").strip().lower()
    review_status = (
        str(updated_evidence.get("bb2_review_request_status") or "").strip().lower()
    )
    if task.status == AgentTaskStatus.READY_FOR_REVIEW and review_status == "completed":
        if decision == "approved":
            packet_id = updated_evidence.get("bb2_review_packet_id")
            mark_agent_task_review_approved(
                task,
                review_packet_id=str(packet_id) if packet_id is not None else None,
            )
        elif decision in {"needs_changes", "rejected"}:
            mark_agent_task_review_changes_requested(task, decision=decision)

    if updated_evidence != evidence:
        task.execution_evidence = updated_evidence
        store.save_agent_task(task)
    return task


def build_agent_bus_review_request_payload(
    task: AgentTask,
    payload: AgentTaskExecutionResult,
    review_dispatch: dict[str, Any],
    *,
    default_review_agent: str = "bb2",
) -> dict[str, Any]:
    review_dispatch = _merge_workflow_chain_review_dispatch(task, review_dispatch)
    reviewer = _first_string(
        review_dispatch,
        "review_agent",
        "target_agent",
        "reviewer",
        "owner_agent",
    ) or default_review_agent
    reviewer = reviewer.strip().lower()
    repo = _first_string(review_dispatch, "repository", "repo") or task.repo_full_name
    branch = _first_string(review_dispatch, "branch") or payload.branch or task.branch
    base_branch = _first_string(review_dispatch, "base_branch")
    pr_number = _first_int(review_dispatch, "pr_number")
    issue_number = _first_int(review_dispatch, "issue_number") or task.issue_number
    evidence_id = _first_string(review_dispatch, "evidence_packet_id", "evidence_id")
    source_work_item_id = _first_string(review_dispatch, "work_item_id") or task.agent_bus_work_item_id
    workflow_chain = review_dispatch.get("workflow_chain") if isinstance(review_dispatch.get("workflow_chain"), dict) else None
    title = _first_string(review_dispatch, "title") or (
        f"BB2 review for {repo} PR #{pr_number}" if pr_number else f"BB2 review for {task.title}"
    )

    metadata = {
        "source": "riseos-agent-orchestrator.agent_task_execution_result",
        "work_item_type": REVIEW_WORK_ITEM_TYPE,
        "task_type": REVIEW_WORK_ITEM_TYPE,
        "queue": REVIEW_QUEUE,
        "reviewer": reviewer,
        "review_agent": reviewer,
        "target_agent": reviewer,
        "requested_by": payload.agent_id,
        "agent_task_id": task.task_id,
        "orchestrator_task_id": task.task_id,
        "workflow_id": task.correlation_id,
        "source_work_item_id": source_work_item_id,
        "implementation_work_item_id": source_work_item_id,
        "evidence_packet_id": evidence_id,
        "evidence_id": evidence_id,
        "repository": repo,
        "repo": repo,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "branch": branch,
        "base_branch": base_branch,
        "commit_sha": payload.commit_sha,
        "changed_files": list(payload.changed_files),
        "workflow_chain": workflow_chain,
        "prompt": _first_string(review_dispatch, "prompt") or "Review Codex worker implementation evidence for this PR.",
        "review_dispatch": _sanitized_review_dispatch(review_dispatch),
    }

    return {
        "title": title,
        "repository": repo,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "owner_agent": reviewer,
        "review_agent": reviewer,
        "metadata": {key: value for key, value in metadata.items() if value is not None},
    }


def _review_dispatch_from_payload(payload: AgentTaskExecutionResult) -> dict[str, Any] | None:
    review_dispatch = payload.evidence.get("review_dispatch")
    if isinstance(review_dispatch, dict) and review_dispatch:
        return review_dispatch
    return None


def _merge_workflow_chain_review_dispatch(task: AgentTask, review_dispatch: dict[str, Any]) -> dict[str, Any]:
    workflow_chain = _workflow_chain_metadata(task)
    if not workflow_chain:
        return review_dispatch
    merged = dict(review_dispatch)
    for key, value in workflow_chain.items():
        merged.setdefault(key, value)
    merged.setdefault("workflow_chain", dict(workflow_chain))
    if "workflow_step" not in merged and workflow_chain.get("current_workflow_step") is not None:
        merged["workflow_step"] = workflow_chain["current_workflow_step"]
    if "current_workflow_step" not in merged and workflow_chain.get("workflow_step") is not None:
        merged["current_workflow_step"] = workflow_chain["workflow_step"]
    return merged


def _workflow_chain_metadata(task: AgentTask) -> dict[str, Any]:
    evidence = task.execution_evidence if isinstance(task.execution_evidence, dict) else {}
    workflow_chain = evidence.get("_workflow_chain") if isinstance(evidence.get("_workflow_chain"), dict) else {}
    allowed = {
        "workflow_chain_id",
        "workflow_family",
        "workflow_sequence",
        "workflow_steps",
        "workflow_step",
        "current_workflow_step",
        "next_workflow_step",
        "final_workflow_step",
        "continuation_mode",
        "merge_gate",
        "repository",
        "base_branch",
    }
    return {key: value for key, value in workflow_chain.items() if key in allowed and value is not None}


def _sanitized_review_dispatch(review_dispatch: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(review_dispatch)
    sanitized.pop("dispatch_prompt", None)
    tools = sanitized.get("tool_preference")
    if isinstance(tools, list):
        sanitized["tool_preference"] = [
            str(tool)
            for tool in tools
            if str(tool) not in {"dispatch_prompt", "mark_ready_for_review"}
        ]
    return sanitized


def _first_string(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (dict, list)):
            text = str(value).strip()
            if text:
                return text
    return None


def _first_int(mapping: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None
