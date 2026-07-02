from __future__ import annotations

from typing import Any


def log_workflow_chain_availability(event_name: str, item: Any, **extra: Any) -> None:
    """Emit temporary diagnostics showing where workflow-chain metadata is present."""
    from app.operational_logging import log_event

    log_event(event_name, **workflow_chain_availability_context(item), **extra)


def log_review_work_item_identity(event_name: str, item: Any, *, caller: str, **extra: Any) -> None:
    from app.operational_logging import log_event

    context = workflow_chain_availability_context(item)
    object_id = id(item)
    context.update(
        {
            "id_review_item": object_id,
            "review_item_object_id": object_id,
            "workflow_chain_length": _workflow_chain_length(item),
            "caller": caller,
        }
    )
    log_event(event_name, **context, **extra)


def workflow_chain_availability_context(item: Any) -> dict[str, Any]:
    runtime_context = _dict_value(_value_from(item, "runtime_validation_context"))
    review_dispatch = _dict_value(runtime_context.get("review_dispatch"))
    metadata = _metadata_from(item, runtime_context, review_dispatch)

    direct_workflow_chain = _dict_value(_value_from(item, "workflow_chain"))
    runtime_workflow_chain = _dict_value(runtime_context.get("workflow_chain"))
    review_workflow_chain = _dict_value(review_dispatch.get("workflow_chain"))
    metadata_workflow_chain = _dict_value(metadata.get("workflow_chain"))

    direct_private_workflow_chain = _dict_value(_value_from(item, "_workflow_chain"))
    runtime_private_workflow_chain = _dict_value(runtime_context.get("_workflow_chain"))
    review_private_workflow_chain = _dict_value(review_dispatch.get("_workflow_chain"))
    metadata_private_workflow_chain = _dict_value(metadata.get("_workflow_chain"))

    workflow_chain = _first_dict(
        direct_workflow_chain,
        runtime_workflow_chain,
        review_workflow_chain,
        metadata_workflow_chain,
    )
    private_workflow_chain = _first_dict(
        direct_private_workflow_chain,
        runtime_private_workflow_chain,
        review_private_workflow_chain,
        metadata_private_workflow_chain,
    )

    return {
        "object_type": type(item).__name__,
        "object_module": type(item).__module__,
        "object_keys": _object_keys(item),
        "work_item_id": _string_or_none(_first_present(_value_from(item, "agent_bus_work_item_id"), _value_from(item, "id"))),
        "review_item_id": _string_or_none(_value_from(item, "id")),
        "runtime_validation_id": _string_or_none(_value_from(item, "runtime_validation_id")),
        "workflow_id": _string_or_none(_first_present(runtime_context.get("workflow_id"), review_dispatch.get("workflow_id"))),
        "repository": _string_or_none(_first_present(_value_from(item, "repo_full_name"), runtime_context.get("repository"), runtime_context.get("repo"), review_dispatch.get("repository"))),
        "pr_number": _first_present(_value_from(item, "pr_number"), runtime_context.get("pr_number"), review_dispatch.get("pr_number")),
        "branch": _string_or_none(_first_present(_value_from(item, "branch"), runtime_context.get("branch"), review_dispatch.get("branch"))),
        "workflow_chain_populated": bool(workflow_chain),
        "_workflow_chain_populated": bool(private_workflow_chain),
        "review_dispatch_populated": bool(review_dispatch),
        "runtime_context_populated": bool(runtime_context),
        "metadata_workflow_chain_populated": bool(metadata_workflow_chain),
        "workflow_chain_keys": sorted(str(key) for key in workflow_chain.keys()),
        "_workflow_chain_keys": sorted(str(key) for key in private_workflow_chain.keys()),
        "review_dispatch_keys": sorted(str(key) for key in review_dispatch.keys()),
        "runtime_context_keys": sorted(str(key) for key in runtime_context.keys()),
        "metadata_keys": sorted(str(key) for key in metadata.keys()),
    }


def _metadata_from(item: Any, *contexts: dict[str, Any]) -> dict[str, Any]:
    direct_metadata = _dict_value(_value_from(item, "metadata"))
    if direct_metadata:
        return direct_metadata
    for context in contexts:
        metadata = _dict_value(context.get("metadata"))
        if metadata:
            return metadata
    return {}


def _workflow_chain_length(item: Any) -> int:
    context = workflow_chain_availability_context(item)
    if context["workflow_chain_populated"]:
        return len(context["workflow_chain_keys"])
    if context["_workflow_chain_populated"]:
        return len(context["_workflow_chain_keys"])
    return 0


def _object_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return sorted(str(key) for key in value.keys())
    fields = getattr(value.__class__, "model_fields", None)
    if isinstance(fields, dict):
        return sorted(str(key) for key in fields.keys())
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        return sorted(str(key) for key in data.keys() if not str(key).startswith("_"))
    return []


def _value_from(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_dict(*values: dict[str, Any]) -> dict[str, Any]:
    for value in values:
        if value:
            return value
    return {}


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
