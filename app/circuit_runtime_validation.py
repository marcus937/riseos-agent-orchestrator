from __future__ import annotations

import hashlib
import ipaddress
import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.config import Settings
from app.hermes_dispatch import (
    HermesDispatchResult,
    HermesEvidenceSnapshot,
    HermesHTTPClient,
    _canonical_hermes_job_id,
    _collect_hermes_evidence,
    _format_optional_bool,
    _redact_sensitive_text,
)

RuntimeValidationStatus = Literal["blocked", "completed", "failed", "pending"]


class RuntimeValidationRequest(BaseModel):
    repo: str
    issue_number: int | None = None
    pr_number: int | None = None
    branch: str | None = None
    target_url: str | None = None
    validation_type: str = "playwright"
    requested_by: str = "circuit"
    correlation_id: str | None = None


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
    validation_type: str
    requested_by: str
    created_at: datetime
    completed_at: datetime | None = None
    correlation_id: str
    hermes: RuntimeValidationHermesSummary
    evidence: RuntimeValidationEvidenceSummary
    bb2: RuntimeValidationBB2Packet
    error: str | None = None


class RuntimeValidationStore:
    def __init__(self) -> None:
        self._items: dict[str, RuntimeValidationResult] = {}

    def get(self, validation_id: str) -> RuntimeValidationResult | None:
        return self._items.get(validation_id)

    async def trigger(self, request: RuntimeValidationRequest, settings: Settings) -> RuntimeValidationResult:
        validation_id = str(uuid.uuid4())
        correlation_id = request.correlation_id or f"runtime-validation-{validation_id[:8]}"
        target_url = request.target_url or settings.hermes_default_target
        created_at = datetime.now(UTC)
        blocked = _target_url_blocker(target_url)
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
                validation_type=request.validation_type,
                requested_by=request.requested_by,
                created_at=created_at,
                completed_at=datetime.now(UTC),
                correlation_id=correlation_id,
                hermes=RuntimeValidationHermesSummary(target_url=_safe_text(target_url, settings), error=blocked),
                evidence=RuntimeValidationEvidenceSummary(),
                bb2=RuntimeValidationBB2Packet(review_status="blocked"),
                error=blocked,
            )
            self._items[validation_id] = result
            return result

        result = RuntimeValidationResult(
            validation_id=validation_id,
            status="pending",
            repo=request.repo,
            issue_number=request.issue_number,
            pr_number=request.pr_number,
            branch=request.branch,
            validation_type=request.validation_type,
            requested_by=request.requested_by,
            created_at=created_at,
            correlation_id=correlation_id,
            hermes=RuntimeValidationHermesSummary(target_url=_safe_text(target_url, settings), target_source="request" if request.target_url else "hermes_default_target"),
            evidence=RuntimeValidationEvidenceSummary(),
            bb2=RuntimeValidationBB2Packet(),
        )
        self._items[validation_id] = result

        hermes_client = HermesHTTPClient()
        try:
            response = await hermes_client.post_job(
                settings.hermes_m2_base_url or "",
                settings.hermes_m2_token or "",
                _build_runtime_payload(request, target_url, correlation_id, settings),
            )
            dispatch = _dispatch_result_from_response(response, target_url=target_url, correlation_id=correlation_id, settings=settings)
            if dispatch.job_id and dispatch.status in {"PASSED", "FAILED"}:
                dispatch.evidence = await _collect_hermes_evidence(
                    hermes_client,
                    settings.hermes_m2_base_url or "",
                    settings.hermes_m2_token or "",
                    dispatch.job_id,
                    settings,
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
        finally:
            await hermes_client.aclose()

        self._items[validation_id] = result
        return result


runtime_validation_store = RuntimeValidationStore()


def _hermes_config_blocker(settings: Settings) -> str | None:
    if not settings.hermes_m2_enable_dispatch:
        return "HERMES_M2_ENABLE_DISPATCH=false."
    if not settings.hermes_m2_base_url or not settings.hermes_m2_token:
        return "Missing HERMES_M2_BASE_URL or HERMES_M2_TOKEN."
    return None


def _target_url_blocker(target_url: str | None) -> str | None:
    if not target_url:
        return "target_url is required."
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "target_url must be an http or https URL."
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0"} or host.endswith(".local"):
        return "target_url must not point at localhost or a local-only host."
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast:
        return "target_url must not point at a private, loopback, link-local, reserved, or multicast address."
    return None


def _build_runtime_payload(
    request: RuntimeValidationRequest,
    target_url: str,
    correlation_id: str,
    settings: Settings,
) -> dict[str, Any]:
    branch = request.branch or settings.work_branch
    payload: dict[str, Any] = {
        "source": "riseos-agent-orchestrator",
        "repo": request.repo,
        "branch": branch,
        "targetUrl": target_url,
        "previewUrl": target_url if _is_vercel_preview_url(target_url) else None,
        "preview_url": target_url if _is_vercel_preview_url(target_url) else None,
        "validationType": request.validation_type,
        "validation_type": request.validation_type,
        "targetSource": "request" if request.target_url else "hermes_default_target",
        "requestedBy": request.requested_by,
        "requested_by": request.requested_by,
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
        "payload": payload,
    }


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
        job_id=_canonical_hermes_job_id(response),
        error=_safe_text(str(response.get("error")), settings) if response.get("error") else None,
    )


def _result_from_dispatch(
    result: RuntimeValidationResult,
    dispatch: HermesDispatchResult,
    settings: Settings,
) -> RuntimeValidationResult:
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
        return _format_optional_bool(value)
    redacted = _redact_sensitive_text(str(value), settings) or ""
    return redacted if len(redacted) <= 500 else redacted[:497] + "..."


def _is_vercel_preview_url(target_url: str) -> bool:
    host = (urlparse(target_url).hostname or "").lower()
    return host == "vercel.app" or host.endswith(".vercel.app")


def stable_validation_digest(result: RuntimeValidationResult) -> str:
    payload = result.model_dump_json(exclude={"created_at", "completed_at"}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
