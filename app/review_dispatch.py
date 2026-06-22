from __future__ import annotations

import json
import logging
from typing import Any

from app.agent_tasks import AgentTask, AgentTaskExecutionResult, AgentTaskStore
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


def build_agent_bus_review_request_payload(
    task: AgentTask,
    payload: AgentTaskExecutionResult,
    review_dispatch: dict[str, Any],
    *,
    default_review_agent: str = "bb2",
) -> dict[str, Any]:
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
