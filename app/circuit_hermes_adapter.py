from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from app.config import Settings
from app.hermes_dispatch import HermesEvidenceArtifact, HermesEvidenceSnapshot, HermesHTTPClient

SECRET_REDACTION = "[REDACTED]"
SECRET_PATTERNS = (
    re.compile(r"(?i)((?:x-hermes-token|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|pwd|secret|client[_-]?secret)\s*[:=]\s*['\"]?)([^'\"\s,;}]+)"),
    re.compile(r"(?i)(bearer\s+)([^'\"\s,;}]+)"),
)


class CircuitHermesClient:
    """Public adapter for Circuit runtime validation Hermes operations."""

    def __init__(self) -> None:
        self._client = HermesHTTPClient()

    async def post_runtime_validation(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._client.post_job(base_url, token, payload)

    async def collect_evidence(
        self,
        base_url: str,
        token: str,
        job_id: str,
        settings: Settings,
    ) -> HermesEvidenceSnapshot | None:
        snapshot = HermesEvidenceSnapshot(job_id=job_id)
        errors: list[str] = []

        try:
            manifest = await self._client.get_evidence_manifest(base_url, token, job_id)
            snapshot.manifest_fetched = True
            snapshot.manifest = manifest if isinstance(manifest, dict) else {"raw": manifest}
            _populate_snapshot_from_manifest(snapshot)
        except Exception as exc:
            errors.append(f"manifest fetch failed: {redact_runtime_text(str(exc), settings)}")

        try:
            bundle = await self._client.get_evidence_bundle(base_url, token, job_id)
            snapshot.bundle_fetched = True
            if isinstance(bundle, dict):
                snapshot.bundle_content_type = _first_string(bundle, "content_type", "contentType", "mimeType")
                snapshot.bundle_size = _first_int(bundle, "size", "contentLength", "content_length", "bytes")
        except Exception as exc:
            errors.append(f"bundle fetch failed: {redact_runtime_text(str(exc), settings)}")

        if errors:
            snapshot.error = "; ".join(error for error in errors if error)
        return snapshot

    async def aclose(self) -> None:
        await self._client.aclose()


def canonical_job_id(response: dict[str, Any]) -> str | None:
    direct = _first_string(response, "jobId", "job_id", "id")
    if direct:
        return direct
    for key in ("job", "data", "payload", "validation"):
        nested = response.get(key)
        if isinstance(nested, dict):
            nested_id = _first_string(nested, "jobId", "job_id", "id")
            if nested_id:
                return nested_id
    return None


def redact_runtime_text(value: str | None, settings: Settings) -> str | None:
    if value is None:
        return None
    redacted = value
    for secret in _known_secret_values(settings):
        redacted = redacted.replace(secret, SECRET_REDACTION)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}{SECRET_REDACTION}", redacted)
    return redacted


def format_optional_bool(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def _populate_snapshot_from_manifest(snapshot: HermesEvidenceSnapshot) -> None:
    manifest = snapshot.manifest
    snapshot.page_title = _deep_first_string(manifest, ("pageTitle", "page_title", "title"))
    snapshot.final_url = _deep_first_string(manifest, ("finalUrl", "final_url", "url"))
    snapshot.http_status = _deep_first_int(manifest, ("httpStatus", "http_status", "statusCode", "status_code", "status"))
    snapshot.console_warning_count = _deep_first_int(manifest, ("consoleWarningCount", "console_warning_count", "warnings", "warningCount"))
    snapshot.console_error_count = _deep_first_int(manifest, ("consoleErrorCount", "console_error_count", "errors", "errorCount"))
    snapshot.network_failure_count = _deep_first_int(manifest, ("networkFailureCount", "network_failure_count", "failedRequests", "failureCount"))
    snapshot.network_non_2xx_count = _deep_first_int(manifest, ("networkNon2xxCount", "network_non_2xx_count", "non2xxCount", "non_2xx_count"))
    _set_extra(snapshot, "viewport", _deep_first(manifest, ("viewport", "viewportSize", "viewport_size")))
    _set_extra(snapshot, "user_agent", _deep_first_string(manifest, ("userAgent", "user_agent")))
    _set_extra(snapshot, "load_duration", _deep_first_int(manifest, ("loadDuration", "load_duration", "loadDurationMs", "load_duration_ms", "durationMs", "duration_ms")))
    _set_extra(snapshot, "console_info_count", _deep_first_int(manifest, ("consoleInfoCount", "console_info_count", "infoCount")))
    _set_extra(snapshot, "console_log_count", _deep_first_int(manifest, ("consoleLogCount", "console_log_count", "logCount")))
    _set_extra(snapshot, "network_request_count", _deep_first_int(manifest, ("requestCount", "request_count", "requestsCount", "requests_count", "totalRequests", "total_requests")))
    _set_extra(snapshot, "network_response_count", _deep_first_int(manifest, ("responseCount", "response_count", "responsesCount", "responses_count", "totalResponses", "total_responses")))
    snapshot.artifacts = _artifacts_from_manifest(manifest, snapshot.job_id)
    snapshot.screenshot_present = any(_is_screenshot_artifact(artifact) for artifact in snapshot.artifacts)


def _artifacts_from_manifest(manifest: dict[str, Any], job_id: str) -> list[HermesEvidenceArtifact]:
    raw_artifacts = manifest.get("artifacts") or manifest.get("files") or manifest.get("evidenceFiles") or []
    if isinstance(raw_artifacts, dict):
        raw_artifacts = [{"name": name, **value} if isinstance(value, dict) else {"name": name, "value": value} for name, value in raw_artifacts.items()]
    artifacts: list[HermesEvidenceArtifact] = []
    if not isinstance(raw_artifacts, list):
        return artifacts
    for item in raw_artifacts:
        if isinstance(item, str):
            artifacts.append(HermesEvidenceArtifact(file_name=PurePosixPath(item).name, retrieval_note=_retrieval_reference(job_id, item)))
        elif isinstance(item, dict):
            file_name = _first_string(item, "fileName", "file_name", "name", "path")
            if file_name:
                artifacts.append(
                    HermesEvidenceArtifact(
                        file_name=PurePosixPath(file_name).name,
                        content_type=_first_string(item, "contentType", "content_type", "mimeType", "mime"),
                        size=_first_int(item, "size", "bytes", "contentLength", "content_length"),
                        sha256=_first_string(item, "sha256", "sha", "digest"),
                        download_url=_first_string(item, "downloadUrl", "download_url", "url"),
                        retrieval_note=_first_string(item, "retrievalNote", "retrieval_note") or _retrieval_reference(job_id, file_name),
                    )
                )
    return artifacts


def _is_screenshot_artifact(artifact: HermesEvidenceArtifact) -> bool:
    file_name = artifact.file_name.lower()
    content_type = artifact.content_type or ""
    return "screenshot" in file_name or file_name.endswith((".png", ".jpg", ".jpeg", ".webp")) or content_type.startswith("image/")


def _retrieval_reference(job_id: str, file_name: str) -> str:
    return f"GET /api/v1/evidence/{job_id}/files/{PurePosixPath(file_name).name}"


def _first_string(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        item = value.get(key)
        if item is not None:
            return str(item)
    return None


def _first_int(value: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            return item
        if isinstance(item, str) and item.isdigit():
            return int(item)
    return None


def _deep_first_string(value: Any, keys: tuple[str, ...]) -> str | None:
    found = _deep_first(value, keys)
    return str(found) if found is not None else None


def _deep_first_int(value: Any, keys: tuple[str, ...]) -> int | None:
    found = _deep_first(value, keys)
    if isinstance(found, bool):
        return None
    if isinstance(found, int):
        return found
    if isinstance(found, str) and found.isdigit():
        return int(found)
    return None


def _deep_first(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return value[key]
        for item in value.values():
            found = _deep_first(item, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _deep_first(item, keys)
            if found is not None:
                return found
    return None


def _set_extra(snapshot: HermesEvidenceSnapshot, name: str, value: Any) -> None:
    if value is not None:
        object.__setattr__(snapshot, name, value)


def _known_secret_values(settings: Settings) -> list[str]:
    names = (
        "github_webhook_secret",
        "github_token",
        "github_app_private_key_path",
        "openai_api_key",
        "orchestrator_admin_token",
        "slack_webhook_url",
        "slack_bot_token",
        "hermes_token",
        "hermes_m2_token",
        "hermes_dgx_token",
    )
    return [value for name in names if isinstance((value := getattr(settings, name, None)), str) and len(value) >= 4]
