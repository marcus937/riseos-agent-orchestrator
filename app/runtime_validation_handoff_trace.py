from __future__ import annotations

import json
from functools import wraps
from typing import Any

import httpx

from app.operational_logging import log_event

_PATCHED = False
HERMES_JOB_ENDPOINT = "/api/v1/jobs"


def install_runtime_validation_handoff_trace() -> None:
    """Install diagnostics around the synchronous runtime validation Hermes handoff."""

    global _PATCHED
    if _PATCHED:
        return

    from app import circuit_runtime_validation as runtime_module
    from app import wf20_runtime_validation as wf20_module
    from app.circuit_hermes_adapter import CircuitHermesClient
    from app.hermes_dispatch_impl import HermesHTTPClient

    _patch_runtime_validation_store(runtime_module)
    _patch_agent_bus_runtime_validation_store(wf20_module)
    _patch_hermes_http_client(HermesHTTPClient)
    _patch_circuit_hermes_client(CircuitHermesClient)
    _PATCHED = True


def _patch_runtime_validation_store(runtime_module: Any) -> None:
    original_trigger = runtime_module.RuntimeValidationStore.trigger

    @wraps(original_trigger)
    async def traced_trigger(self: Any, request: Any, settings: Any) -> Any:
        trace = _request_fields(request)
        log_event(
            "runtime_validation_trigger_boundary_entered",
            **trace,
            selected_store_class=self.__class__.__name__,
            selected_store_module=self.__class__.__module__,
            hermes_base_url=settings.hermes_m2_base_url,
            hermes_endpoint_path=HERMES_JOB_ENDPOINT,
            hermes_m2_enable_dispatch=settings.hermes_m2_enable_dispatch,
            hermes_base_url_configured=bool(settings.hermes_m2_base_url),
            hermes_token_configured=bool(settings.hermes_m2_token),
            target_url=getattr(request, "target_url", None),
        )
        result = await original_trigger(self, request, settings)
        result_trace = _result_fields(result)
        skip_block_reason = _result_skip_or_block_reason(result)
        log_event(
            "runtime_validation_created",
            **result_trace,
            selected_store_class=self.__class__.__name__,
            selected_store_module=self.__class__.__module__,
            hermes_base_url=settings.hermes_m2_base_url,
            hermes_endpoint_path=HERMES_JOB_ENDPOINT,
            runtime_validation_status=getattr(result, "status", None),
            hermes_status=getattr(getattr(result, "hermes", None), "status", None),
            hermes_job_id=getattr(getattr(result, "hermes", None), "job_id", None),
            skip_block_reason=skip_block_reason,
        )
        log_event(
            "runtime_validation_trigger_boundary_completed",
            **result_trace,
            selected_store_class=self.__class__.__name__,
            selected_store_module=self.__class__.__module__,
            hermes_base_url=settings.hermes_m2_base_url,
            hermes_endpoint_path=HERMES_JOB_ENDPOINT,
            runtime_validation_status=getattr(result, "status", None),
            hermes_status=getattr(getattr(result, "hermes", None), "status", None),
            hermes_job_id=getattr(getattr(result, "hermes", None), "job_id", None),
            skip_block_reason=skip_block_reason,
        )
        if skip_block_reason:
            log_event(
                "runtime_validation_handoff_terminal",
                **result_trace,
                terminal_reason=_terminal_reason(result),
                skip_block_reason=skip_block_reason,
            )
        return result

    runtime_module.RuntimeValidationStore.trigger = traced_trigger


def _patch_agent_bus_runtime_validation_store(wf20_module: Any) -> None:
    original_trigger = wf20_module.AgentBusRuntimeValidationStore.trigger
    original_record = wf20_module._record_agent_bus_state

    @wraps(original_trigger)
    async def traced_agent_bus_trigger(self: Any, request: Any, settings: Any) -> Any:
        trace = _request_fields(request)
        log_event(
            "agent_bus_runtime_validation_store_entered",
            **trace,
            selected_store_class=self.__class__.__name__,
            selected_store_module=self.__class__.__module__,
            hermes_base_url=settings.hermes_m2_base_url,
            hermes_endpoint_path=HERMES_JOB_ENDPOINT,
            hermes_m2_enable_dispatch=settings.hermes_m2_enable_dispatch,
            hermes_base_url_configured=bool(settings.hermes_m2_base_url),
            hermes_token_configured=bool(settings.hermes_m2_token),
            target_url=getattr(request, "target_url", None),
        )
        result = await original_trigger(self, request, settings)
        log_event(
            "agent_bus_runtime_validation_store_completed",
            **_result_fields(result),
            selected_store_class=self.__class__.__name__,
            selected_store_module=self.__class__.__module__,
            hermes_base_url=settings.hermes_m2_base_url,
            hermes_endpoint_path=HERMES_JOB_ENDPOINT,
            runtime_validation_status=getattr(result, "status", None),
            hermes_status=getattr(getattr(result, "hermes", None), "status", None),
            hermes_job_id=getattr(getattr(result, "hermes", None), "job_id", None),
            skip_block_reason=_result_skip_or_block_reason(result),
        )
        return result

    @wraps(original_record)
    async def traced_record_agent_bus_state(*args: Any, **kwargs: Any) -> Any:
        request = args[1] if len(args) > 1 else kwargs.get("request")
        state = args[2] if len(args) > 2 else kwargs.get("state")
        runtime_result = kwargs.get("runtime_result")
        log_event(
            "agent_bus_runtime_validation_write_started",
            **_request_fields(request),
            runtime_validation_id=getattr(runtime_result, "validation_id", None),
            lookup_key="AgentBusClient.record_runtime_validation",
            runtime_state=getattr(state, "value", state),
            target_url=getattr(request, "target_url", None),
        )
        result = await original_record(*args, **kwargs)
        log_event(
            "agent_bus_runtime_validation_write_completed",
            **_request_fields(request),
            runtime_validation_id=getattr(runtime_result, "validation_id", None),
            lookup_key="AgentBusClient.record_runtime_validation",
            runtime_state=getattr(state, "value", state),
            lookup_result=_compact_mapping(result),
        )
        return result

    wf20_module.AgentBusRuntimeValidationStore.trigger = traced_agent_bus_trigger
    wf20_module._record_agent_bus_state = traced_record_agent_bus_state


def _patch_hermes_http_client(hermes_http_client_cls: Any) -> None:
    async def traced_post_job(self: Any, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        trace = _payload_fields(payload)
        endpoint_url = f"{base_url.rstrip('/')}{HERMES_JOB_ENDPOINT}"
        log_event(
            "hermes_dispatch_started",
            **trace,
            hermes_base_url=base_url,
            hermes_endpoint_path=HERMES_JOB_ENDPOINT,
            target_url=payload.get("targetUrl") or payload.get("payload", {}).get("targetUrl"),
            payload_type=payload.get("type"),
            dry_run=payload.get("dryRun"),
        )
        try:
            response = await self._http_client.post(endpoint_url, headers={"X-Hermes-Token": token}, json=payload)
            body_summary = _response_body_summary(response)
            response.raise_for_status()
            if not response.content:
                data: dict[str, Any] = {}
            else:
                raw_data = response.json()
                data = raw_data if isinstance(raw_data, dict) else {"raw": raw_data}
            log_event(
                "hermes_dispatch_completed",
                **trace,
                hermes_base_url=base_url,
                hermes_endpoint_path=HERMES_JOB_ENDPOINT,
                hermes_http_status=response.status_code,
                hermes_response_body_summary=body_summary,
                hermes_job_id=_first_string(data, "jobId", "job_id", "id"),
                hermes_status=data.get("status") or data.get("result"),
            )
            return data
        except Exception as exc:
            status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None else None
            body_summary = _response_body_summary(exc.response) if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None else None
            log_event(
                "hermes_dispatch_failed",
                **trace,
                hermes_base_url=base_url,
                hermes_endpoint_path=HERMES_JOB_ENDPOINT,
                hermes_http_status=status_code,
                hermes_response_body_summary=body_summary,
                exception_source=type(exc).__name__,
                skip_block_reason=str(exc),
            )
            raise

    hermes_http_client_cls.post_job = traced_post_job


def _patch_circuit_hermes_client(circuit_hermes_client_cls: Any) -> None:
    original_collect_evidence = circuit_hermes_client_cls.collect_evidence

    @wraps(original_collect_evidence)
    async def traced_collect_evidence(self: Any, base_url: str, token: str, job_id: str, settings: Any) -> Any:
        endpoint_path = f"/api/v1/evidence/{job_id}"
        log_event(
            "hermes_evidence_collection_boundary_started",
            hermes_base_url=base_url,
            hermes_endpoint_path=endpoint_path,
            hermes_job_id=job_id,
        )
        evidence = await original_collect_evidence(self, base_url, token, job_id, settings)
        log_event(
            "hermes_evidence_collected",
            hermes_base_url=base_url,
            hermes_endpoint_path=endpoint_path,
            hermes_job_id=job_id,
            manifest_fetched=bool(evidence and getattr(evidence, "manifest_fetched", False)),
            bundle_fetched=bool(evidence and getattr(evidence, "bundle_fetched", False)),
            evidence_error=getattr(evidence, "error", None) if evidence else None,
        )
        return evidence

    circuit_hermes_client_cls.collect_evidence = traced_collect_evidence


def _request_fields(request: Any) -> dict[str, Any]:
    review_dispatch = getattr(request, "review_dispatch", None)
    if not isinstance(review_dispatch, dict):
        review_dispatch = {}
    return {
        "workflow_id": getattr(request, "workflow_id", None) or review_dispatch.get("workflow_id"),
        "work_item_id": getattr(request, "work_item_id", None) or review_dispatch.get("work_item_id"),
        "runtime_validation_id": getattr(request, "validation_id", None),
        "repository": getattr(request, "repo", None) or review_dispatch.get("repository") or review_dispatch.get("repo"),
        "pr_number": getattr(request, "pr_number", None) or review_dispatch.get("pr_number"),
        "branch": getattr(request, "branch", None) or review_dispatch.get("branch"),
        "commit_sha": getattr(request, "commit_sha", None) or review_dispatch.get("commit_sha"),
        "evidence_packet_id": getattr(request, "evidence_id", None) or review_dispatch.get("evidence_packet_id") or review_dispatch.get("evidence_id"),
    }


def _result_fields(result: Any) -> dict[str, Any]:
    review_dispatch = getattr(result, "review_dispatch", None)
    if not isinstance(review_dispatch, dict):
        review_dispatch = {}
    hermes = getattr(result, "hermes", None)
    return {
        "workflow_id": getattr(result, "workflow_id", None) or review_dispatch.get("workflow_id"),
        "work_item_id": getattr(result, "work_item_id", None) or review_dispatch.get("work_item_id"),
        "runtime_validation_id": getattr(result, "validation_id", None),
        "repository": getattr(result, "repo", None) or review_dispatch.get("repository") or review_dispatch.get("repo"),
        "pr_number": getattr(result, "pr_number", None) or review_dispatch.get("pr_number"),
        "branch": getattr(result, "branch", None) or review_dispatch.get("branch"),
        "commit_sha": review_dispatch.get("commit_sha"),
        "target_url": getattr(hermes, "target_url", None),
        "evidence_packet_id": getattr(result, "evidence_id", None) or review_dispatch.get("evidence_packet_id") or review_dispatch.get("evidence_id"),
    }


def _payload_fields(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    review_dispatch = payload.get("reviewDispatch") or payload.get("review_dispatch") or nested.get("reviewDispatch") or nested.get("review_dispatch") or {}
    if not isinstance(review_dispatch, dict):
        review_dispatch = {}
    return {
        "workflow_id": payload.get("workflowId") or payload.get("workflow_id") or nested.get("workflowId") or nested.get("workflow_id") or review_dispatch.get("workflow_id"),
        "work_item_id": payload.get("workItemId") or payload.get("work_item_id") or nested.get("workItemId") or nested.get("work_item_id") or review_dispatch.get("work_item_id"),
        "runtime_validation_id": payload.get("validationId") or payload.get("validation_id") or nested.get("validationId") or nested.get("validation_id"),
        "repository": nested.get("repo") or nested.get("repository") or review_dispatch.get("repository") or review_dispatch.get("repo"),
        "pr_number": nested.get("prNumber") or nested.get("pr_number") or review_dispatch.get("pr_number"),
        "branch": nested.get("branch") or review_dispatch.get("branch"),
        "commit_sha": nested.get("commitSha") or nested.get("commit_sha") or review_dispatch.get("commit_sha"),
        "evidence_packet_id": payload.get("evidenceId") or payload.get("evidence_id") or nested.get("evidenceId") or nested.get("evidence_id") or review_dispatch.get("evidence_packet_id") or review_dispatch.get("evidence_id"),
    }


def _result_skip_or_block_reason(result: Any) -> str | None:
    hermes = getattr(result, "hermes", None)
    return getattr(result, "error", None) or getattr(hermes, "error", None)


def _terminal_reason(result: Any) -> str:
    status = getattr(result, "status", None)
    hermes = getattr(result, "hermes", None)
    hermes_status = getattr(hermes, "status", None)
    if status == "pending":
        return "HERMES_SKIPPED_PENDING_DEPLOYMENT"
    if status == "failed":
        return "HERMES_DISPATCH_FAILED"
    if status == "blocked" or hermes_status in {"BLOCKED", "SKIPPED"}:
        return "HERMES_NOT_CALLED_BLOCKED"
    return "HERMES_HANDOFF_TERMINAL"


def _response_body_summary(response: Any) -> Any:
    text = getattr(response, "text", None)
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:1000]
    if isinstance(payload, dict):
        return {key: payload.get(key) for key in sorted(payload)[:20]}
    return payload


def _compact_mapping(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {key: value.get(key) for key in ("id", "runtime_validation_id", "work_item_id", "state", "status", "current_state") if value.get(key) is not None}


def _first_string(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        item = value.get(key)
        if item is not None:
            return str(item)
    return None
