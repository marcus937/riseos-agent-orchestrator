from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import uuid
from datetime import UTC, datetime
from typing import Any, Callable, Literal, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.circuit_hermes_adapter import CircuitHermesClient, canonical_job_id, format_optional_bool, redact_runtime_text
from app.config import Settings
from app.hermes_dispatch import HermesDispatchResult, HermesEvidenceSnapshot
from app.operational_logging import log_event

RuntimeValidationStatus = Literal["blocked", "completed", "failed", "pending"]


class RuntimeValidationRequest(BaseModel):
    repo: str
    issue_number: int | None = None
    pr_number: int | None = None
    branch: str | None = None
    base_branch: str | None = None
    target_url: str | None = None
    target_url_source: str | None = None
    target_url_pending_reason: str | None = None
    validation_type: str = "playwright"
    requested_by: str = "circuit"
    correlation_id: str | None = None
    work_item_id: str | None = None
    evidence_id: str | None = None
    review_agent: str | None = None
    workflow_id: str | None = None
    review_dispatch: dict[str, Any] = Field(default_factory=dict)


class RuntimeValidationHermesSummary(BaseModel):
    job_id: str | None = None
    target_url: str | None = None
    target_source: str | None = None
    status: str = "SKIPPED"
    manifest_fetched: bool = False
    bundle_fetched: bool = False
    error: str | None = None


class RuntimeValidationEvidenceSummary(BaseModel):
    page_title: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    viewport: Any | None = None
    user_agent: str | None = None
    load_duration: Any | None = None
    console_warning_count: int | None = None
    console_error_count: int | None = None
    console_info_count: int | None = None
    console_log_count: int | None = None
    network_request_count: int | None = None
    network_response_count: int | None = None
    network_failure_count: int | None = None
    network_non_2xx_count: int | None = None
    screenshot_present: bool | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class RuntimeValidationBB2Packet(BaseModel):
    packet_created: bool = False
    review_requested: bool = False
    review_status: Literal["approved", "needs_changes", "blocked", "pending"] = "pending"
    review_context: dict[str, Any] = Field(default_factory=dict)


class RuntimeValidationResult(BaseModel):
    validation_id: str
    status: RuntimeValidationStatus
    repo: str
    issue_number: int | None = None
    pr_number: int | None = None
    branch: str | None = None
    base_branch: str | None = None
    work_item_id: str | None = None
    evidence_id: str | None = None
    review_agent: str | None = None
    workflow_id: str | None = None
    review_dispatch: dict[str, Any] = Field(default_factory=dict)
    validation_type: str
    requested_by: str
    created_at: datetime
    completed_at: datetime | None = None
    correlation_id: str
    hermes: RuntimeValidationHermesSummary
    evidence: RuntimeValidationEvidenceSummary
    bb2: RuntimeValidationBB2Packet
    error: str | None = None


class RuntimeHermesClient(Protocol):
    async def post_runtime_validation(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def collect_evidence(
        self,
        base_url: str,
        token: str,
        job_id: str,
        settings: Settings,
    ) -> HermesEvidenceSnapshot | None: ...

    async def aclose(self) -> None: ...


class RuntimeValidationStore:
    def __init__(self, hermes_client_factory: Callable[[], RuntimeHermesClient] = CircuitHermesClient) -> None:
        self._items: dict[str, RuntimeValidationResult] = {}
        self._hermes_client_factory = hermes_client_factory

    def get(self, validation_id: str) -> RuntimeValidationResult | None:
        return self._items.get(validation_id)

    async def trigger(self, request: RuntimeValidationRequest, settings: Settings) -> RuntimeValidationResult:
        validation_id = str(uuid.uuid4())
        correlation_id = request.correlation_id or f"runtime-validation-{validation_id[:8]}"
        target_url = request.target_url
        created_at = datetime.now(UTC)
        target_source = request.target_url_source or ("request" if request.target_url else "missing")
        review_dispatch = _build_review_dispatch_payload(request, correlation_id)
        log_event(
            "runtime_validation_trigger_started",
            validation_id=validation_id,
            correlation_id=correlation_id,
            repo=request.repo,
            issue_number=request.issue_number,
            pr_number=request.pr_number,
            branch=request.branch,
            base_branch=request.base_branch,
            work_item_id=request.work_item_id,
            evidence_id=request.evidence_id,
            review_agent=request.review_agent,
            workflow_id=request.workflow_id,
            validation_type=request.validation_type,
            requested_by=request.requested_by,
            target_url_source=target_source,
        )

        if target_url is None and target_source == "vercel_preview_pending":
            result = RuntimeValidationResult(
                validation_id=validation_id,
                status="pending",
                repo=request.repo,
                issue_number=request.issue_number,
                pr_number=request.pr_number,
                branch=request.branch,
                base_branch=request.base_branch,
                work_item_id=request.work_item_id,
                evidence_id=request.evidence_id,
                review_agent=request.review_agent,
                workflow_id=request.workflow_id,
                review_dispatch=review_dispatch,
                validation_type=request.validation_type,
                requested_by=request.requested_by,
                created_at=created_at,
                correlation_id=correlation_id,
                hermes=RuntimeValidationHermesSummary(
                    target_url=None,
                    target_source=target_source,
                    status="SKIPPED",
                    error=request.target_url_pending_reason,
                ),
                evidence=RuntimeValidationEvidenceSummary(),
                bb2=RuntimeValidationBB2Packet(review_status="pending"),
                error=request.target_url_pending_reason,
            )
            self._items[validation_id] = result
            log_event(
                "runtime_validation_preview_pending",
                validation_id=validation_id,
                correlation_id=correlation_id,
                repo=request.repo,
                pr_number=request.pr_number,
                branch=request.branch,
                base_branch=request.base_branch,
                target_url_source=target_source,
                fallback_reason=request.target_url_pending_reason,
            )
            return result

        target_url = target_url or settings.hermes_default_target
        blocked = _target_url_blocker(target_url, settings)
        if blocked is None:
            blocked = _hermes_config_blocker(settings)
        if blocked is not None:
            result = RuntimeValidationResult(
                validation_id=validation_id,
                status="blocked",
                repo=request.repo,
                issue_number=request.issue_number,
                pr_number=request.pr_number,
                branch=request.branch,
                base_branch=request.base_branch,
                work_item_id=request.work_item_id,
                evidence_id=request.evidence_id,
                review_agent=request.review_agent,
                workflow_id=request.workflow_id,
                review_dispatch=review_dispatch,
                validation_type=request.validation_type,
                requested_by=request.requested_by,
                created_at=created_at,
                completed_at=datetime.now(UTC),
                correlation_id=correlation_id,
                hermes=RuntimeValidationHermesSummary(target_url=_safe_text(target_url, settings), target_source=target_source, error=blocked),
                evidence=RuntimeValidationEvidenceSummary(),
                bb2=RuntimeValidationBB2Packet(review_status="blocked"),
                error=blocked,
            )
            self._items[validation_id] = result
            log_event(
                "runtime_validation_trigger_blocked",
                validation_id=validation_id,
                correlation_id=correlation_id,
                repo=request.repo,
                pr_number=request.pr_number,
                branch=request.branch,
                base_branch=request.base_branch,
                target_url_source=target_source,
                error=blocked,
            )
            return result

        result = RuntimeValidationResult(
            validation_id=validation_id,
            status="pending",
            repo=request.repo,
            issue_number=request.issue_number,
            pr_number=request.pr_number,
            branch=request.branch,
            base_branch=request.base_branch,
            work_item_id=request.work_item_id,
            evidence_id=request.evidence_id,
            review_agent=request.review_agent,
            workflow_id=request.workflow_id,
            review_dispatch=review_dispatch,
            validation_type=request.validation_type,
            requested_by=request.requested_by,
            created_at=created_at,
            correlation_id=correlation_id,
            hermes=RuntimeValidationHermesSummary(target_url=_safe_text(target_url, settings), target_source=target_source),
            evidence=RuntimeValidationEvidenceSummary(),
            bb2=RuntimeValidationBB2Packet(),
        )
        self._items[validation_id] = result

        hermes_client = self._hermes_client_factory()
        try:
            payload = _build_runtime_payload(request, target_url, correlation_id, settings, target_source=target_source)
            log_event(
                "hermes_runtime_validation_post_started",
                validation_id=validation_id,
                correlation_id=correlation_id,
                hermes_base_url=settings.hermes_m2_base_url,
                repo=request.repo,
                pr_number=request.pr_number,
                branch=request.branch,
                base_branch=request.base_branch,
                work_item_id=request.work_item_id,
                evidence_id=request.evidence_id,
                review_agent=request.review_agent,
                workflow_id=request.workflow_id,
                validation_type=request.validation_type,
                target_url_source=target_source,
            )
            response = await hermes_client.post_runtime_validation(
                settings.hermes_m2_base_url or "",
                settings.hermes_m2_token or "",
                payload,
            )
            log_event(
                "hermes_runtime_validation_post_completed",
                validation_id=validation_id,
                correlation_id=correlation_id,
                status=response.get("status") or response.get("result"),
                job_id=canonical_job_id(response),
                response_keys=sorted(str(key) for key in response.keys()),
            )
            dispatch = _dispatch_result_from_response(response, target_url=target_url, correlation_id=correlation_id, settings=settings)
            if dispatch.job_id and dispatch.status in {"PASSED", "FAILED"}:
                log_event(
                    "hermes_evidence_collection_started",
                    validation_id=validation_id,
                    correlation_id=correlation_id,
                    job_id=dispatch.job_id,
                )
                dispatch.evidence = await hermes_client.collect_evidence(
                    settings.hermes_m2_base_url or "",
                    settings.hermes_m2_token or "",
                    dispatch.job_id,
                    settings,
                )
                log_event(
                    "hermes_evidence_collection_completed",
                    validation_id=validation_id,
                    correlation_id=correlation_id,
                    job_id=dispatch.job_id,
                    manifest_fetched=bool(dispatch.evidence and dispatch.evidence.manifest_fetched),
                    bundle_fetched=bool(dispatch.evidence and dispatch.evidence.bundle_fetched),
                )
            result = _result_from_dispatch(result, dispatch, settings)
        except Exception as exc:
            error = _safe_text(str(exc), settings)
            result.status = "failed"
            result.completed_at = datetime.now(UTC)
            result.error = error
            result.hermes.status = "BLOCKED"
            result.hermes.error = error
            result.bb2 = RuntimeValidationBB2Packet(review_status="blocked")
            log_event(
                "runtime_validation_trigger_failed",
                validation_id=validation_id,
                correlation_id=correlation_id,
                repo=request.repo,
                pr_number=request.pr_number,
                branch=request.branch,
                base_branch=request.base_branch,
                error=error,
            )
        finally:
            await hermes_client.aclose()

        self._items[validation_id] = result
        log_event(
            "runtime_validation_trigger_completed",
            validation_id=validation_id,
            correlation_id=correlation_id,
            repo=result.repo,
            pr_number=result.pr_number,
            branch=result.branch,
            base_branch=result.base_branch,
            status=result.status,
            hermes_status=result.hermes.status,
            bb2_review_status=result.bb2.review_status,
        )
        return result


runtime_validation_store = RuntimeValidationStore()


def _hermes_config_blocker(settings: Settings) -> str | None:
    if not settings.hermes_m2_enable_dispatch:
        return "HERMES_M2_ENABLE_DISPATCH=false."
    if not settings.hermes_m2_base_url or not settings.hermes_m2_token:
        return "Missing HERMES_M2_BASE_URL or HERMES_M2_TOKEN."
    return None


def _target_url_blocker(target_url: str | None, settings: Settings) -> str | None:
    if not target_url:
        return "target_url is required."
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "target_url must be an http or https URL."
    if parsed.username or parsed.password:
        return "target_url must not include embedded credentials."
    host = (parsed.hostname or "").lower()
    if not _host_allowed(host, settings):
        return "target_url host must be a trusted Vercel preview host or the configured Hermes default target host."
    if host in {"localhost", "127.0.0.1", "0.0.0.0"} or host.endswith(".local"):
        return "target_url must not point at localhost or a local-only host."
    literal_error = _ip_address_blocker(host)
    if literal_error is not None:
        return literal_error
    return _dns_resolution_blocker(host)


def _host_allowed(host: str, settings: Settings) -> bool:
    if _is_vercel_preview_host(host):
        return True
    default_host = (urlparse(settings.hermes_default_target).hostname or "").lower()
    return bool(default_host and host == default_host)


def _ip_address_blocker(host: str) -> str | None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    return _unsafe_address_error(address)


def _dns_resolution_blocker(host: str) -> str | None:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        return f"target_url host could not be resolved safely: {exc}"
    addresses = {info[4][0] for info in infos if info and len(info) >= 5 and info[4]}
    if not addresses:
        return "target_url host did not resolve to an IP address."
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            return f"target_url host resolved to an invalid IP address: {raw_address}"
        error = _unsafe_address_error(address)
        if error is not None:
            return error
    return None


def _unsafe_address_error(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast:
        return "target_url must not resolve to a private, loopback, link-local, reserved, or multicast address."
    return None


def _build_runtime_payload(
    request: RuntimeValidationRequest,
    target_url: str,
    correlation_id: str,
    settings: Settings,
    *,
    target_source: str,
) -> dict[str, Any]:
    branch = request.branch or settings.work_branch
    review_dispatch = _build_review_dispatch_payload(request, correlation_id)
    payload: dict[str, Any] = {
        "source": "riseos-agent-orchestrator",
        "repo": request.repo,
        "branch": branch,
        "baseBranch": request.base_branch,
        "base_branch": request.base_branch,
        "targetUrl": target_url,
        "previewUrl": target_url if _is_vercel_preview_url(target_url) else None,
        "preview_url": target_url if _is_vercel_preview_url(target_url) else None,
        "validationType": request.validation_type,
        "validation_type": request.validation_type,
        "targetSource": target_source,
        "target_url_source": target_source,
        "requestedBy": request.requested_by,
        "requested_by": request.requested_by,
        "workItemId": request.work_item_id,
        "work_item_id": request.work_item_id,
        "evidenceId": request.evidence_id,
        "evidence_id": request.evidence_id,
        "reviewAgent": request.review_agent,
        "review_agent": request.review_agent,
        "workflowId": request.workflow_id,
        "workflow_id": request.workflow_id,
        "reviewDispatch": review_dispatch,
        "review_dispatch": review_dispatch,
        "hermesNode": "M2",
        "trigger": "circuit_runtime_validation_api",
    }
    if request.issue_number is not None:
        payload["issueNumber"] = request.issue_number
    if request.pr_number is not None:
        payload["prNumber"] = request.pr_number
        payload["pr_number"] = request.pr_number
    return {
        "type": request.validation_type,
        "dryRun": False,
        "targetUrl": target_url,
        "validation_type": request.validation_type,
        "correlationId": correlation_id,
        "workItemId": request.work_item_id,
        "work_item_id": request.work_item_id,
        "evidenceId": request.evidence_id,
        "evidence_id": request.evidence_id,
        "reviewAgent": request.review_agent,
        "review_agent": request.review_agent,
        "workflowId": request.workflow_id,
        "workflow_id": request.workflow_id,
        "reviewDispatch": review_dispatch,
        "review_dispatch": review_dispatch,
        "payload": payload,
    }


def _build_review_dispatch_payload(request: RuntimeValidationRequest, correlation_id: str) -> dict[str, Any]:
    review_agent = request.review_agent or request.review_dispatch.get("review_agent") or request.review_dispatch.get("target_agent") or "bb2"
    pr_number = request.pr_number or request.review_dispatch.get("pr_number")
    title = request.review_dispatch.get("title") or (
        f"BB2 review for {request.repo} PR #{pr_number}" if pr_number else f"BB2 review for {request.repo}"
    )
    prompt = request.review_dispatch.get("prompt") or "Review Codex worker implementation evidence for this PR."
    payload = {
        **request.review_dispatch,
        "repository": request.review_dispatch.get("repository") or request.repo,
        "repo": request.review_dispatch.get("repo") or request.repo,
        "title": title,
        "prompt": prompt,
        "issue_number": request.issue_number if request.issue_number is not None else request.review_dispatch.get("issue_number"),
        "pr_number": pr_number,
        "branch": request.branch or request.review_dispatch.get("branch"),
        "base_branch": request.base_branch or request.review_dispatch.get("base_branch"),
        "work_item_id": request.work_item_id or request.review_dispatch.get("work_item_id"),
        "evidence_id": request.evidence_id or request.review_dispatch.get("evidence_id"),
        "evidence_packet_id": request.evidence_id or request.review_dispatch.get("evidence_packet_id"),
        "owner_agent": request.review_dispatch.get("owner_agent") or review_agent,
        "reviewer": request.review_dispatch.get("reviewer") or review_agent,
        "review_agent": review_agent,
        "target_agent": request.review_dispatch.get("target_agent") or review_agent,
        "requested_by": request.review_dispatch.get("requested_by") or request.requested_by,
        "correlation_id": request.review_dispatch.get("correlation_id") or correlation_id,
        "workflow_id": request.workflow_id or request.review_dispatch.get("workflow_id"),
        "source": request.review_dispatch.get("source") or "riseos-agent-orchestrator",
    }
    if "tool_preference" not in payload:
        payload["tool_preference"] = ["create_review_packet", "attach_review_to_work_item", "mark_ready_for_review", "dispatch_prompt"]
    return {key: value for key, value in payload.items() if value is not None}


def _dispatch_result_from_response(
    response: dict[str, Any],
    *,
    target_url: str,
    correlation_id: str,
    settings: Settings,
) -> HermesDispatchResult:
    status_value = str(response.get("status") or response.get("result") or "PASSED").upper()
    if status_value in {"FAILED", "FAIL"}:
        status: Literal["FAILED", "PASSED", "BLOCKED", "SKIPPED"] = "FAILED"
        success = False
    elif status_value in {"BLOCKED", "ERROR"}:
        status = "BLOCKED"
        success = False
    else:
        status = "PASSED"
        success = True
    return HermesDispatchResult(
        attempted=True,
        success=success,
        status=status,
        hermes_node="M2",
        correlation_id=correlation_id,
        target_url=_safe_text(target_url, settings),
        target_source="runtime_validation_api",
        job_id=canonical_job_id(response),
        error=_safe_text(str(response.get("error")), settings) if response.get("error") else None,
    )


def _result_from_dispatch(result: RuntimeValidationResult, dispatch: HermesDispatchResult, settings: Settings) -> RuntimeValidationResult:
    evidence = _evidence_summary(dispatch.evidence, settings)
    review_status: Literal["approved", "needs_changes", "blocked", "pending"] = "pending"
    if dispatch.status == "PASSED":
        review_status = "approved"
    elif dispatch.status == "FAILED":
        review_status = "needs_changes"
    elif dispatch.status == "BLOCKED":
        review_status = "blocked"
    result.status = "completed" if dispatch.status in {"PASSED", "FAILED"} else "blocked"
    result.completed_at = datetime.now(UTC)
    result.error = dispatch.error
    result.hermes = RuntimeValidationHermesSummary(
        job_id=dispatch.job_id,
        target_url=_safe_text(dispatch.target_url, settings),
        target_source=dispatch.target_source,
        status=dispatch.status,
        manifest_fetched=bool(dispatch.evidence and dispatch.evidence.manifest_fetched),
        bundle_fetched=bool(dispatch.evidence and dispatch.evidence.bundle_fetched),
        error=dispatch.error,
    )
    result.evidence = evidence
    result.bb2 = RuntimeValidationBB2Packet(
        packet_created=True,
        review_requested=False,
        review_status=review_status,
        review_context={
            "source": "circuit_runtime_validation_api",
            "correlation_id": result.correlation_id,
            "work_item_id": result.work_item_id,
            "evidence_id": result.evidence_id,
            "review_agent": result.review_agent,
            "workflow_id": result.workflow_id,
            "review_dispatch": result.review_dispatch,
            "field_propagation_matrix": _field_matrix(evidence),
        },
    )
    return result


def _evidence_summary(evidence: HermesEvidenceSnapshot | None, settings: Settings) -> RuntimeValidationEvidenceSummary:
    if evidence is None:
        return RuntimeValidationEvidenceSummary()
    artifacts = []
    for artifact in evidence.artifacts:
        artifacts.append(
            {
                "file_name": _safe_text(artifact.file_name, settings),
                "content_type": _safe_text(artifact.content_type, settings),
                "size": artifact.size,
                "sha256": artifact.sha256,
                "retrieval": _safe_text(artifact.retrieval_note, settings),
            }
        )
    return RuntimeValidationEvidenceSummary(
        page_title=_safe_text(evidence.page_title, settings),
        final_url=_safe_text(evidence.final_url, settings),
        http_status=evidence.http_status,
        viewport=_safe_value(getattr(evidence, "viewport", None), settings),
        user_agent=_safe_text(getattr(evidence, "user_agent", None), settings),
        load_duration=_safe_value(getattr(evidence, "load_duration", None), settings),
        console_warning_count=evidence.console_warning_count,
        console_error_count=evidence.console_error_count,
        console_info_count=getattr(evidence, "console_info_count", None),
        console_log_count=getattr(evidence, "console_log_count", None),
        network_request_count=getattr(evidence, "network_request_count", None),
        network_response_count=getattr(evidence, "network_response_count", None),
        network_failure_count=evidence.network_failure_count,
        network_non_2xx_count=evidence.network_non_2xx_count,
        screenshot_present=evidence.screenshot_present,
        artifacts=artifacts,
        error=_safe_text(evidence.error, settings),
    )


def _field_matrix(evidence: RuntimeValidationEvidenceSummary) -> dict[str, bool]:
    return {
        "page_title": evidence.page_title is not None,
        "final_url": evidence.final_url is not None,
        "http_status": evidence.http_status is not None,
        "viewport": evidence.viewport is not None,
        "user_agent": evidence.user_agent is not None,
        "load_duration": evidence.load_duration is not None,
        "console_warning_count": evidence.console_warning_count is not None,
        "console_error_count": evidence.console_error_count is not None,
        "console_info_count": evidence.console_info_count is not None,
        "console_log_count": evidence.console_log_count is not None,
        "network_request_count": evidence.network_request_count is not None,
        "network_response_count": evidence.network_response_count is not None,
        "network_failure_count": evidence.network_failure_count is not None,
        "network_non_2xx_count": evidence.network_non_2xx_count is not None,
        "screenshot_present": evidence.screenshot_present is not None,
        "artifact_size_sha_metadata": any(item.get("size") is not None or item.get("sha256") for item in evidence.artifacts),
    }


def _safe_value(value: Any, settings: Settings) -> Any | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return _safe_text(str(value), settings) if isinstance(value, str) else value
    if isinstance(value, dict):
        return {str(key): _safe_value(item, settings) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_value(item, settings) for item in value]
    return _safe_text(str(value), settings)


def _safe_text(value: Any, settings: Settings) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return format_optional_bool(value)
    redacted = redact_runtime_text(str(value), settings) or ""
    return redacted if len(redacted) <= 500 else redacted[:497] + "..."


def _is_vercel_preview_url(target_url: str) -> bool:
    host = (urlparse(target_url).hostname or "").lower()
    return _is_vercel_preview_host(host)


def _is_vercel_preview_host(host: str) -> bool:
    return host == "vercel.app" or host.endswith(".vercel.app")


def stable_validation_digest(result: RuntimeValidationResult) -> str:
    payload = json.dumps(
        result.model_dump(mode="json", exclude={"created_at", "completed_at"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()