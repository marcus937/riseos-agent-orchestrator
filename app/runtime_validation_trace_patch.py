from __future__ import annotations

from functools import wraps
from typing import Any, Awaitable, Callable

from app.runtime_validation_trace import (
    REJECTION_MESSAGE,
    item_trace_fields,
    request_trace_fields,
    result_trace_fields,
    trace_runtime_validation_lookup,
)

_PATCHED = False


def install_runtime_validation_trace_patch() -> None:
    """Install diagnostics-only wrappers around runtime validation review lookups."""

    global _PATCHED
    if _PATCHED:
        return

    from app import circuit_runtime_validation as runtime_module
    from app import runtime_validation_review_bridge as bridge_module
    from app import review_queue as review_queue_module
    from app import wf20_runtime_validation as wf20_module

    _patch_runtime_store(runtime_module)
    _patch_agent_bus_runtime_state(wf20_module)
    _patch_review_bridge(bridge_module)
    _patch_review_gate(review_queue_module)
    _PATCHED = True


def _patch_runtime_store(runtime_module: Any) -> None:
    original_trigger = runtime_module.RuntimeValidationStore.trigger
    original_get = runtime_module.RuntimeValidationStore.get

    @wraps(original_trigger)
    async def traced_trigger(self: Any, request: Any, settings: Any) -> Any:
        trace_runtime_validation_lookup(
            "runtime_validation_creation",
            **request_trace_fields(request),
            lookup_key="POST /api/v1/runtime-validations",
            lookup_result="started",
            validation_type=getattr(request, "validation_type", None),
        )
        result = await original_trigger(self, request, settings)
        trace_runtime_validation_lookup(
            "runtime_validation_persistence",
            **result_trace_fields(result),
            lookup_key="RuntimeValidationStore._items[validation_id]",
            lookup_result=getattr(result, "status", None),
            missing_field=_missing_result_field(result),
            hermes_status=getattr(getattr(result, "hermes", None), "status", None),
            bb2_review_status=getattr(getattr(result, "bb2", None), "review_status", None),
        )
        return result

    def traced_get(self: Any, validation_id: str) -> Any:
        result = original_get(self, validation_id)
        trace_runtime_validation_lookup(
            "runtime_validation_store_get",
            **(result_trace_fields(result) if result is not None else {"runtime_validation_id": validation_id}),
            lookup_key=f"validation_id={validation_id}",
            lookup_result="found" if result is not None else "missing",
            missing_field=None if result is not None else "runtime_validation_id",
        )
        return result

    runtime_module.RuntimeValidationStore.trigger = traced_trigger
    runtime_module.RuntimeValidationStore.get = traced_get


def _patch_agent_bus_runtime_state(wf20_module: Any) -> None:
    original_record = wf20_module._record_agent_bus_state

    @wraps(original_record)
    async def traced_record_agent_bus_state(*args: Any, **kwargs: Any) -> Any:
        request = args[1] if len(args) > 1 else kwargs.get("request")
        state = args[2] if len(args) > 2 else kwargs.get("state")
        runtime_result = kwargs.get("runtime_result")
        trace_runtime_validation_lookup(
            "agent_bus_runtime_validation_persistence",
            **request_trace_fields(request),
            lookup_key=f"work_item_id={getattr(request, 'work_item_id', None)} state={getattr(state, 'value', state)}",
            lookup_result="started",
            missing_field=_missing_request_field(request),
            state=getattr(state, "value", state),
            runtime_validation_id=getattr(runtime_result, "validation_id", None),
        )
        result = await original_record(*args, **kwargs)
        trace_runtime_validation_lookup(
            "agent_bus_runtime_validation_persistence_completed",
            **request_trace_fields(request),
            lookup_key="AgentBusClient.record_runtime_validation",
            lookup_result="recorded" if result is not None else "skipped",
            missing_field=None if result is not None else "work_item_id",
            state=getattr(state, "value", state),
            runtime_validation_id=getattr(runtime_result, "validation_id", None),
            connector_result=_compact_lookup_result(result),
        )
        if runtime_result is not None:
            trace_runtime_validation_lookup(
                "runtime_validation_evidence_attachment",
                **result_trace_fields(runtime_result),
                lookup_key="runtime_result.evidence -> Agent Bus runtime validation payload",
                lookup_result="attached" if result is not None else "missing_work_item",
                missing_field=_missing_evidence_field(runtime_result),
                state=getattr(state, "value", state),
            )
        return result

    wf20_module._record_agent_bus_state = traced_record_agent_bus_state


def _patch_review_bridge(bridge_module: Any) -> None:
    original_enqueue = bridge_module.enqueue_review_from_runtime_validation
    original_find_exact = bridge_module._find_exact_runtime_result
    original_find_pending = bridge_module._find_pending_runtime_result
    original_find_existing = bridge_module._find_existing_runtime_item
    original_attach = bridge_module._attach_runtime_validation_context

    @wraps(original_find_exact)
    def traced_find_exact(result: Any, *, digest: str, storage: Any | None = None) -> Any:
        trace_runtime_validation_lookup(
            "review_gate_exact_runtime_result_lookup",
            **result_trace_fields(result),
            lookup_key=f"validation_id={getattr(result, 'validation_id', None)} digest={digest}",
            lookup_result="started",
            missing_field=_missing_result_field(result),
        )
        item = original_find_exact(result, digest=digest, storage=storage)
        trace_runtime_validation_lookup(
            "review_gate_exact_runtime_result_lookup_completed",
            **(item_trace_fields(item) if item is not None else result_trace_fields(result)),
            lookup_key=f"validation_id={getattr(result, 'validation_id', None)} digest={digest}",
            lookup_result="found" if item is not None else "missing",
            missing_field=None if item is not None else "runtime_validation_id_or_digest",
        )
        return item

    @wraps(original_find_pending)
    def traced_find_pending(result: Any, *, storage: Any | None = None) -> Any:
        trace_runtime_validation_lookup(
            "review_gate_pending_work_item_lookup",
            **result_trace_fields(result),
            lookup_key=_pending_runtime_lookup_key(result),
            lookup_result="started",
            missing_field=_missing_pending_lookup_field(result),
        )
        item = original_find_pending(result, storage=storage)
        trace_runtime_validation_lookup(
            "review_gate_pending_work_item_lookup_completed",
            **(item_trace_fields(item) if item is not None else result_trace_fields(result)),
            lookup_key=_pending_runtime_lookup_key(result),
            lookup_result="found" if item is not None else "missing",
            missing_field=None if item is not None else _missing_pending_lookup_field(result),
        )
        return item

    @wraps(original_find_existing)
    def traced_find_existing(item: Any, *, storage: Any | None = None) -> Any:
        trace_runtime_validation_lookup(
            "runtime_validation_work_item_association_lookup",
            **item_trace_fields(item),
            lookup_key="review_work_item_identity",
            lookup_result="started",
            missing_field=_missing_item_identity_field(item),
        )
        duplicate = original_find_existing(item, storage=storage)
        trace_runtime_validation_lookup(
            "runtime_validation_work_item_association_lookup_completed",
            **(item_trace_fields(duplicate) if duplicate is not None else item_trace_fields(item)),
            lookup_key="review_work_item_identity",
            lookup_result="found" if duplicate is not None else "missing",
            missing_field=None if duplicate is not None else _missing_item_identity_field(item),
        )
        return duplicate

    @wraps(original_attach)
    def traced_attach(item: Any, result: Any, *, digest: str) -> None:
        trace_runtime_validation_lookup(
            "runtime_validation_context_attachment",
            **result_trace_fields(result),
            lookup_key=f"review_item_id={getattr(item, 'id', None)}",
            lookup_result="started",
            missing_field=_missing_result_field(result),
        )
        original_attach(item, result, digest=digest)
        trace_runtime_validation_lookup(
            "runtime_validation_context_attachment_completed",
            **item_trace_fields(item),
            lookup_key=f"review_item_id={getattr(item, 'id', None)}",
            lookup_result="attached",
            missing_field=_missing_runtime_context_field(item),
        )

    @wraps(original_enqueue)
    def traced_enqueue(result: Any, settings: Any, *, storage: Any | None = None, existing_item: Any | None = None) -> Any:
        trace_runtime_validation_lookup(
            "runtime_validation_review_bridge_enqueue",
            **result_trace_fields(result),
            lookup_key="enqueue_review_from_runtime_validation",
            lookup_result="started",
            missing_field=_missing_result_field(result),
        )
        item = original_enqueue(result, settings, storage=storage, existing_item=existing_item)
        trace_runtime_validation_lookup(
            "runtime_validation_review_bridge_enqueue_completed",
            **(item_trace_fields(item) if item is not None else result_trace_fields(result)),
            lookup_key="enqueue_review_from_runtime_validation",
            lookup_result="queued" if item is not None else "skipped",
            missing_field=None if item is not None else "review_work_item",
        )
        return item

    bridge_module._find_exact_runtime_result = traced_find_exact
    bridge_module._find_pending_runtime_result = traced_find_pending
    bridge_module._find_existing_runtime_item = traced_find_existing
    bridge_module._attach_runtime_validation_context = traced_attach
    bridge_module.enqueue_review_from_runtime_validation = traced_enqueue


def _patch_review_gate(review_queue_module: Any) -> None:
    original_blocked_reason = review_queue_module._blocked_reason

    @wraps(original_blocked_reason)
    def traced_blocked_reason(item: Any) -> str | None:
        trace_runtime_validation_lookup(
            "review_gate_lookup",
            **item_trace_fields(item),
            lookup_key="ReviewWorkItem.runtime_validation_context",
            lookup_result="started",
            missing_field=_missing_runtime_context_field(item),
        )
        reason = original_blocked_reason(item)
        missing_runtime_field = _missing_runtime_context_field(item)
        if reason:
            trace_runtime_validation_lookup(
                "review_gate_rejection",
                **item_trace_fields(item),
                lookup_key="ReviewWorkItem.blocked_reason",
                lookup_result="rejected",
                missing_field=missing_runtime_field,
                rejection_reason=reason,
            )
        elif missing_runtime_field:
            trace_runtime_validation_lookup(
                "review_gate_required_evidence_missing",
                **item_trace_fields(item),
                lookup_key="ReviewWorkItem.runtime_validation_context",
                lookup_result="missing",
                missing_field=missing_runtime_field,
                rejection_reason=REJECTION_MESSAGE,
            )
        else:
            trace_runtime_validation_lookup(
                "review_gate_lookup_completed",
                **item_trace_fields(item),
                lookup_key="ReviewWorkItem.runtime_validation_context",
                lookup_result="found",
            )
        return reason

    review_queue_module._blocked_reason = traced_blocked_reason


def _missing_request_field(request: Any) -> str | None:
    for field in ("work_item_id", "workflow_id", "repo", "pr_number", "branch"):
        if not getattr(request, field, None):
            return field
    return None


def _missing_result_field(result: Any) -> str | None:
    for field in ("validation_id", "work_item_id", "workflow_id", "repo", "pr_number", "branch"):
        if not getattr(result, field, None):
            return field
    return None


def _missing_pending_lookup_field(result: Any) -> str | None:
    for field in ("repo", "pr_number", "branch"):
        if not getattr(result, field, None):
            return field
    return None


def _missing_item_identity_field(item: Any) -> str | None:
    for field in ("repo_full_name", "event_type"):
        if not getattr(item, field, None):
            return field
    if not getattr(item, "commit_sha", None) and getattr(item, "pr_number", None) is None and getattr(item, "issue_number", None) is None:
        return "commit_sha_or_pr_number_or_issue_number"
    return None


def _missing_runtime_context_field(item: Any) -> str | None:
    context = getattr(item, "runtime_validation_context", None)
    if not isinstance(context, dict) or not context:
        return "runtime_validation_context"
    for field in ("validation_id", "validation_status", "hermes_status", "evidence"):
        if not context.get(field):
            return field
    evidence = context.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        return "evidence"
    return None


def _missing_evidence_field(result: Any) -> str | None:
    evidence = getattr(result, "evidence", None)
    if evidence is None:
        return "evidence"
    for field in ("final_url", "http_status", "artifacts"):
        value = getattr(evidence, field, None)
        if value in (None, [], {}):
            return field
    return None


def _pending_runtime_lookup_key(result: Any) -> str:
    return " ".join(
        [
            f"repo={getattr(result, 'repo', None)}",
            f"pr={getattr(result, 'pr_number', None)}",
            f"issue={getattr(result, 'issue_number', None)}",
            f"branch={getattr(result, 'branch', None)}",
            "status=runtime_validation_pending",
        ]
    )


def _compact_lookup_result(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {key: value.get(key) for key in ("id", "runtime_validation_id", "work_item_id", "state", "status", "current_state") if value.get(key) is not None}
