from __future__ import annotations

from typing import Any

from app.operational_logging import log_event

TRACE_EVENT = "runtime_validation_review_gate_trace"
REJECTION_MESSAGE = "Hermes Playwright validation evidence is required before frontend work can enter review."


def trace_runtime_validation_lookup(
    stage: str,
    *,
    runtime_validation_id: str | None = None,
    work_item_id: str | None = None,
    workflow_id: str | None = None,
    evidence_packet_id: str | None = None,
    repository: str | None = None,
    commit_sha: str | None = None,
    pr_number: int | None = None,
    lookup_key: str | None = None,
    lookup_result: str | bool | None = None,
    missing_field: str | None = None,
    rejection_reason: str | None = None,
    **extra: Any,
) -> None:
    """Emit a normalized diagnostics-only trace for runtime validation lookups."""

    log_event(
        TRACE_EVENT,
        stage=stage,
        runtime_validation_id=runtime_validation_id,
        work_item_id=work_item_id,
        workflow_id=workflow_id,
        evidence_packet_id=evidence_packet_id,
        repository=repository,
        commit_sha=commit_sha,
        pr_number=pr_number,
        lookup_key=lookup_key,
        lookup_result=lookup_result,
        missing_field=missing_field,
        rejection_reason=rejection_reason,
        **extra,
    )


def result_trace_fields(result: Any) -> dict[str, Any]:
    review_dispatch = getattr(result, "review_dispatch", None)
    if not isinstance(review_dispatch, dict):
        review_dispatch = {}
    return {
        "runtime_validation_id": getattr(result, "validation_id", None),
        "work_item_id": getattr(result, "work_item_id", None) or review_dispatch.get("work_item_id"),
        "workflow_id": getattr(result, "workflow_id", None) or review_dispatch.get("workflow_id"),
        "evidence_packet_id": getattr(result, "evidence_id", None)
        or review_dispatch.get("evidence_packet_id")
        or review_dispatch.get("evidence_id"),
        "repository": getattr(result, "repo", None) or review_dispatch.get("repository") or review_dispatch.get("repo"),
        "commit_sha": review_dispatch.get("commit_sha"),
        "pr_number": getattr(result, "pr_number", None) or review_dispatch.get("pr_number"),
    }


def request_trace_fields(request: Any) -> dict[str, Any]:
    review_dispatch = getattr(request, "review_dispatch", None)
    if not isinstance(review_dispatch, dict):
        review_dispatch = {}
    return {
        "work_item_id": getattr(request, "work_item_id", None) or review_dispatch.get("work_item_id"),
        "workflow_id": getattr(request, "workflow_id", None) or review_dispatch.get("workflow_id"),
        "evidence_packet_id": getattr(request, "evidence_id", None)
        or review_dispatch.get("evidence_packet_id")
        or review_dispatch.get("evidence_id"),
        "repository": getattr(request, "repo", None) or review_dispatch.get("repository") or review_dispatch.get("repo"),
        "commit_sha": getattr(request, "commit_sha", None) or review_dispatch.get("commit_sha"),
        "pr_number": getattr(request, "pr_number", None) or review_dispatch.get("pr_number"),
    }


def item_trace_fields(item: Any) -> dict[str, Any]:
    context = getattr(item, "runtime_validation_context", None)
    if not isinstance(context, dict):
        context = {}
    return {
        "runtime_validation_id": getattr(item, "runtime_validation_id", None) or context.get("validation_id"),
        "work_item_id": getattr(item, "agent_bus_work_item_id", None) or getattr(item, "id", None),
        "workflow_id": context.get("workflow_id"),
        "evidence_packet_id": context.get("evidence_id") or context.get("evidence_packet_id"),
        "repository": getattr(item, "repo_full_name", None) or context.get("repo"),
        "commit_sha": getattr(item, "commit_sha", None) or context.get("commit_sha"),
        "pr_number": getattr(item, "pr_number", None) or context.get("pr_number"),
    }
