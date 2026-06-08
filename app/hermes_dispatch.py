from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import quote

from app import hermes_dispatch_impl as _impl

ARTIFACT_JSON_FILES = {"summary.json", "page.json", "console.json", "network.json", "logs.json"}
MAX_PACKET_EXCERPTS = 3
ARTIFACT_SECRET_PATTERNS = (
    re.compile(r"(?i)((?:password|passwd|pwd|secret|client[_-]?secret)\s*[:=]\s*['\"]?)([^'\"\s,;}]+)"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token)\s*[:=]\s*['\"]?)([^'\"\s,;}]+)"),
)


def _canonical_hermes_job_id(response: dict[str, Any]) -> str | None:
    direct = _impl._first_string(response, "jobId", "job_id", "id")
    if direct:
        return direct
    for key in ("job", "data", "payload", "validation"):
        nested = response.get(key)
        if isinstance(nested, dict):
            nested_id = _impl._first_string(nested, "jobId", "job_id", "id")
            if nested_id:
                return nested_id
    return None


class HermesHTTPClient(_impl.HermesHTTPClient):
    async def get_evidence_bundle(self, base_url: str, token: str, job_id: str) -> dict[str, Any]:
        response = await self._http_client.get(
            f"{base_url.rstrip('/')}/api/v1/evidence/{job_id}/bundle",
            headers={"X-Hermes-Token": token},
        )
        response.raise_for_status()
        content = response.content or b""
        return {"content_type": response.headers.get("content-type"), "size": len(content), "content": content}

    async def get_evidence_file(self, base_url: str, token: str, job_id: str, file_name: str) -> dict[str, Any]:
        response = await self._http_client.get(
            f"{base_url.rstrip('/')}/api/v1/evidence/{job_id}/files/{quote(file_name, safe='')}",
            headers={"X-Hermes-Token": token},
        )
        response.raise_for_status()
        content = response.content or b""
        return {
            "file_name": file_name,
            "content_type": response.headers.get("content-type"),
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest() if content else None,
            "content": content,
        }


def _result_from_hermes_response(
    response: dict[str, Any],
    *,
    node: Literal["M2", "DGX"],
    dispatch_key: str,
    correlation_id: str,
) -> _impl.HermesDispatchResult:
    status_value = str(response.get("status") or response.get("result") or "PASSED").upper()
    job_id = _canonical_hermes_job_id(response)
    if status_value in {"FAILED", "FAIL"}:
        status: Literal["FAILED", "PASSED", "BLOCKED", "SKIPPED"] = "FAILED"
        label = "agent-revisions"
        success = False
    elif status_value in {"BLOCKED", "ERROR"}:
        status = "BLOCKED"
        label = "agent-blocked"
        success = False
    else:
        status = "PASSED"
        label = "agent-verified"
        success = True
    return _impl.HermesDispatchResult(
        attempted=True,
        success=success,
        status=status,
        hermes_node=node,
        dispatch_key=dispatch_key,
        correlation_id=correlation_id,
        label=label,
        job_id=job_id,
    )


async def _collect_hermes_evidence(
    hermes_client: _impl.HermesDispatchHTTPClient,
    base_url: str,
    token: str,
    job_id: str,
    settings: _impl.Settings,
) -> _impl.HermesEvidenceSnapshot | None:
    get_manifest = getattr(hermes_client, "get_evidence_manifest", None)
    get_bundle = getattr(hermes_client, "get_evidence_bundle", None)
    get_file = getattr(hermes_client, "get_evidence_file", None)
    if get_manifest is None and get_bundle is None and get_file is None:
        return None

    snapshot = _impl.HermesEvidenceSnapshot(job_id=job_id)
    artifact_jsons: dict[str, Any] = {}
    errors: list[str] = []

    if get_manifest is not None:
        try:
            manifest = await get_manifest(base_url, token, job_id)
            snapshot.manifest_fetched = True
            snapshot.manifest = manifest if isinstance(manifest, dict) else {"raw": manifest}
            _impl._populate_evidence_from_manifest(snapshot)
        except Exception as exc:
            errors.append(f"manifest fetch failed: {_impl._redact_sensitive_text(str(exc), settings)}")

    if get_bundle is not None:
        try:
            bundle = await get_bundle(base_url, token, job_id)
            snapshot.bundle_fetched = True
            if isinstance(bundle, dict):
                snapshot.bundle_content_type = _impl._first_string(bundle, "content_type", "contentType", "mimeType")
                snapshot.bundle_size = _impl._first_int(bundle, "size", "contentLength", "content_length", "bytes")
                artifact_jsons.update(_hydrate_snapshot_from_bundle(snapshot, bundle))
        except Exception as exc:
            errors.append(f"bundle fetch failed: {_impl._redact_sensitive_text(str(exc), settings)}")

    if get_file is not None:
        for file_name in _artifact_json_file_names(snapshot, artifact_jsons):
            try:
                artifact_file = await get_file(base_url, token, job_id, file_name)
                _merge_file_artifact_metadata(snapshot, file_name, artifact_file)
                parsed = _parse_json_artifact_response(artifact_file)
                if parsed is not None:
                    artifact_jsons[file_name] = parsed
            except Exception as exc:
                errors.append(f"artifact fetch failed ({file_name}): {_impl._redact_sensitive_text(str(exc), settings)}")

    if artifact_jsons:
        _populate_evidence_from_artifact_jsons(snapshot, artifact_jsons)
    if errors:
        snapshot.error = "; ".join(error for error in errors if error)
    return snapshot


def _hydrate_snapshot_from_bundle(snapshot: _impl.HermesEvidenceSnapshot, bundle: dict[str, Any]) -> dict[str, Any]:
    content = _response_content(bundle)
    if not content:
        return {}
    artifact_jsons, artifact_entries = _parse_bundle_artifacts(content)
    if artifact_entries:
        _merge_artifact_inventory(snapshot, artifact_entries)
    return artifact_jsons


def _parse_bundle_artifacts(content: bytes) -> tuple[dict[str, Any], list[tuple[str, int, str | None]]]:
    artifact_jsons: dict[str, Any] = {}
    artifact_entries: list[tuple[str, int, str | None]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                file_name = PurePosixPath(info.filename).name
                raw = archive.read(info)
                artifact_entries.append((file_name, info.file_size, hashlib.sha256(raw).hexdigest() if raw else None))
                if file_name in ARTIFACT_JSON_FILES:
                    try:
                        artifact_jsons[file_name] = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        pass
    except zipfile.BadZipFile:
        try:
            artifact_jsons["summary.json"] = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return artifact_jsons, artifact_entries


def _merge_artifact_inventory(snapshot: _impl.HermesEvidenceSnapshot, artifact_entries: list[tuple[str, int, str | None]]) -> None:
    existing = {artifact.file_name: artifact for artifact in snapshot.artifacts}
    for file_name, size, sha256 in artifact_entries:
        artifact = existing.get(file_name)
        if artifact is None:
            artifact = _impl.HermesEvidenceArtifact(
                file_name=file_name,
                content_type=_content_type_for_artifact(file_name),
                size=size,
                sha256=sha256,
                retrieval_note=_retrieval_reference(snapshot.job_id, file_name),
            )
            snapshot.artifacts.append(artifact)
            existing[file_name] = artifact
        else:
            artifact.size = artifact.size or size
            artifact.sha256 = artifact.sha256 or sha256
            artifact.content_type = artifact.content_type or _content_type_for_artifact(file_name)
            artifact.retrieval_note = _retrieval_reference(snapshot.job_id, file_name)
    if any(_is_screenshot_artifact(file_name) for file_name, _size, _sha in artifact_entries):
        snapshot.screenshot_present = True


def _merge_file_artifact_metadata(snapshot: _impl.HermesEvidenceSnapshot, file_name: str, artifact_file: Any) -> None:
    if not isinstance(artifact_file, dict):
        return
    content = _response_content(artifact_file)
    sha256 = _impl._first_string(artifact_file, "sha256", "sha", "digest")
    if sha256 is None and content:
        sha256 = hashlib.sha256(content).hexdigest()
    content_type = _impl._first_string(artifact_file, "content_type", "contentType", "mimeType") or _content_type_for_artifact(file_name)
    size = _impl._first_int(artifact_file, "size", "contentLength", "content_length", "bytes")
    if size is None and content is not None:
        size = len(content)
    existing = {artifact.file_name: artifact for artifact in snapshot.artifacts}
    artifact = existing.get(file_name)
    if artifact is None:
        snapshot.artifacts.append(
            _impl.HermesEvidenceArtifact(
                file_name=file_name,
                content_type=content_type,
                size=size,
                sha256=sha256,
                retrieval_note=_retrieval_reference(snapshot.job_id, file_name),
            )
        )
    else:
        artifact.content_type = artifact.content_type or content_type
        artifact.size = artifact.size or size
        artifact.sha256 = artifact.sha256 or sha256
        artifact.retrieval_note = _retrieval_reference(snapshot.job_id, file_name)


def _artifact_json_file_names(snapshot: _impl.HermesEvidenceSnapshot, artifact_jsons: dict[str, Any]) -> list[str]:
    names = [PurePosixPath(artifact.file_name).name for artifact in snapshot.artifacts]
    names.extend(file_name for file_name in _impl.EVIDENCE_FILES if file_name.endswith(".json"))
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name in ARTIFACT_JSON_FILES and name not in artifact_jsons and name not in seen:
            ordered.append(name)
            seen.add(name)
    return ordered


def _parse_json_artifact_response(response: Any) -> Any | None:
    content = _response_content(response)
    if content is not None:
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    if isinstance(response, dict):
        for key in ("json", "body", "data", "raw", "content"):
            value = response.get(key)
            if isinstance(value, (dict, list)):
                return value
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return None
    return None


def _response_content(response: Any) -> bytes | None:
    if isinstance(response, bytes):
        return response
    if isinstance(response, bytearray):
        return bytes(response)
    if isinstance(response, str):
        return response.encode("utf-8")
    if isinstance(response, dict):
        for key in ("content", "body", "data", "raw", "bytes"):
            value = response.get(key)
            if isinstance(value, bytes):
                return value
            if isinstance(value, bytearray):
                return bytes(value)
            if isinstance(value, str):
                return value.encode("utf-8")
    return None


def _content_type_for_artifact(file_name: str) -> str | None:
    lowered = file_name.lower()
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lowered.endswith(".webp"):
        return "image/webp"
    return None


def _is_screenshot_artifact(file_name: str) -> bool:
    lowered = file_name.lower()
    return "screenshot" in lowered or lowered.endswith((".png", ".jpg", ".jpeg", ".webp"))


def _populate_evidence_from_artifact_jsons(snapshot: _impl.HermesEvidenceSnapshot, artifact_jsons: dict[str, Any]) -> None:
    summary = artifact_jsons.get("summary.json")
    page = artifact_jsons.get("page.json")
    console = artifact_jsons.get("console.json")
    network = artifact_jsons.get("network.json")
    logs = artifact_jsons.get("logs.json")

    page_sources = [source for source in (page, summary, snapshot.manifest) if source is not None]
    snapshot.page_title = snapshot.page_title or _first_deep_string(page_sources, ("pageTitle", "page_title", "title"))
    snapshot.final_url = snapshot.final_url or _first_deep_string(page_sources, ("finalUrl", "final_url", "url"))
    snapshot.http_status = snapshot.http_status if snapshot.http_status is not None else _first_deep_int(page_sources, ("httpStatus", "http_status", "statusCode", "status_code", "status"))
    _set_extra(snapshot, "viewport", _first_deep_value(page_sources, ("viewport", "viewportSize", "viewport_size")))
    _set_extra(snapshot, "user_agent", _first_deep_string(page_sources, ("userAgent", "user_agent")))
    _set_extra(snapshot, "load_duration", _first_deep_int(page_sources, ("loadDuration", "load_duration", "loadDurationMs", "load_duration_ms", "durationMs", "duration_ms", "loadTimeMs", "load_time_ms")))

    console_sources = [console, logs, summary, snapshot.manifest]
    snapshot.console_warning_count = _coalesce_int(snapshot.console_warning_count, _first_deep_int(console_sources, ("consoleWarningCount", "console_warning_count", "warningCount", "warnings")), _count_console_entries(console, {"warning", "warn"}), _count_console_entries(logs, {"warning", "warn"}))
    snapshot.console_error_count = _coalesce_int(snapshot.console_error_count, _first_deep_int(console_sources, ("consoleErrorCount", "console_error_count", "errorCount", "errors")), _count_console_entries(console, {"error"}), _count_console_entries(logs, {"error"}))
    _set_extra(snapshot, "console_info_count", _coalesce_int(_get_extra(snapshot, "console_info_count"), _first_deep_int(console_sources, ("consoleInfoCount", "console_info_count", "infoCount", "infos")), _count_console_entries(console, {"info"}), _count_console_entries(logs, {"info"})))
    _set_extra(snapshot, "console_log_count", _coalesce_int(_get_extra(snapshot, "console_log_count"), _first_deep_int(console_sources, ("consoleLogCount", "console_log_count", "logCount", "logs")), _count_console_entries(console, {"log"}), _count_console_entries(logs, {"log"})))
    _set_extra(snapshot, "console_warning_excerpts", _console_excerpts([console, logs], {"warning", "warn"}))
    _set_extra(snapshot, "console_error_excerpts", _console_excerpts([console, logs], {"error"}))

    network_sources = [network, summary, snapshot.manifest]
    network_entries = _as_list(network, "requests", "entries", "network", "events")
    snapshot.network_failure_count = _coalesce_int(snapshot.network_failure_count, _first_deep_int(network_sources, ("networkFailureCount", "network_failure_count", "failedRequests", "failureCount", "failures")), _count_network_failures(network))
    snapshot.network_non_2xx_count = _coalesce_int(snapshot.network_non_2xx_count, _first_deep_int(network_sources, ("networkNon2xxCount", "network_non_2xx_count", "non2xxCount", "non_2xx_count", "non2xx")), _count_network_non_2xx(network))
    _set_extra(snapshot, "network_request_count", _coalesce_int(_get_extra(snapshot, "network_request_count"), _first_deep_int(network_sources, ("requestCount", "request_count", "requestsCount", "requests_count", "totalRequests", "total_requests")), len(network_entries) if network_entries is not None else None))
    _set_extra(snapshot, "network_response_count", _coalesce_int(_get_extra(snapshot, "network_response_count"), _first_deep_int(network_sources, ("responseCount", "response_count", "responsesCount", "responses_count", "totalResponses", "total_responses")), _count_network_responses(network_entries)))
    _set_extra(snapshot, "network_failed_requests", _network_request_excerpts(network_entries, failures=True))
    _set_extra(snapshot, "network_non_2xx_requests", _network_request_excerpts(network_entries, non_2xx=True))


def _first_deep_string(values: list[Any], keys: tuple[str, ...]) -> str | None:
    for value in values:
        found = _impl._deep_first_string(value, keys)
        if found:
            return found
    return None


def _first_deep_int(values: list[Any], keys: tuple[str, ...]) -> int | None:
    for value in values:
        found = _impl._deep_first_int(value, keys)
        if found is not None:
            return found
    return None


def _first_deep_value(values: list[Any], keys: tuple[str, ...]) -> Any | None:
    for value in values:
        found = _impl._deep_first(value, keys)
        if found is not None:
            return found
    return None


def _coalesce_int(*values: int | None) -> int | None:
    for value in values:
        if value is not None:
            return value
    return None


def _count_console_entries(value: Any, levels: set[str]) -> int | None:
    entries = _as_list(value, "messages", "entries", "console", "logs", "events")
    if entries is None:
        return None
    return sum(1 for item in entries if isinstance(item, dict) and str(item.get("level") or item.get("type") or item.get("method") or "").lower() in levels)


def _console_excerpts(values: list[Any], levels: set[str]) -> list[str]:
    excerpts: list[str] = []
    for value in values:
        entries = _as_list(value, "messages", "entries", "console", "logs", "events")
        if entries is None:
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            level = str(item.get("level") or item.get("type") or item.get("method") or "").lower()
            if level not in levels:
                continue
            text = _impl._first_string(item, "text", "message", "value", "description")
            if text:
                excerpts.append(_truncate_packet_text(text))
            if len(excerpts) >= MAX_PACKET_EXCERPTS:
                return excerpts
    return excerpts


def _count_network_failures(value: Any) -> int | None:
    entries = _as_list(value, "requests", "entries", "network", "events")
    if entries is None:
        return None
    return sum(1 for item in entries if isinstance(item, dict) and _network_failed(item))


def _count_network_non_2xx(value: Any) -> int | None:
    entries = _as_list(value, "requests", "entries", "network", "events")
    if entries is None:
        return None
    count = 0
    for item in entries:
        if not isinstance(item, dict):
            continue
        status = _impl._first_int(item, "status", "statusCode", "status_code", "httpStatus", "http_status")
        if status is not None and not 200 <= status <= 299:
            count += 1
    return count


def _count_network_responses(entries: list[Any] | None) -> int | None:
    if entries is None:
        return None
    return sum(1 for item in entries if isinstance(item, dict) and _impl._first_int(item, "status", "statusCode", "status_code", "httpStatus", "http_status") is not None)


def _network_failed(item: dict[str, Any]) -> bool:
    return bool(item.get("failed") or item.get("failure") or item.get("error") or item.get("failureText") or item.get("failure_text"))


def _network_request_excerpts(entries: list[Any] | None, *, failures: bool = False, non_2xx: bool = False) -> list[str]:
    if entries is None:
        return []
    excerpts: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        status = _impl._first_int(item, "status", "statusCode", "status_code", "httpStatus", "http_status")
        include = failures and _network_failed(item)
        include = include or (non_2xx and status is not None and not 200 <= status <= 299)
        if not include:
            continue
        url = _impl._first_string(item, "url", "requestUrl", "request_url", "finalUrl", "final_url") or "unknown-url"
        reason = _impl._first_string(item, "error", "failureText", "failure_text", "message")
        pieces = [url]
        if status is not None:
            pieces.append(f"status={status}")
        if reason:
            pieces.append(reason)
        excerpts.append(_truncate_packet_text(" ".join(pieces)))
        if len(excerpts) >= MAX_PACKET_EXCERPTS:
            return excerpts
    return excerpts


def _as_list(value: Any, *keys: str) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if isinstance(item, list):
                return item
    return None


def _set_extra(snapshot: _impl.HermesEvidenceSnapshot, name: str, value: Any) -> None:
    if value is not None:
        object.__setattr__(snapshot, name, value)


def _get_extra(snapshot: _impl.HermesEvidenceSnapshot, name: str) -> Any | None:
    return getattr(snapshot, name, None)


def _redact_artifact_text(value: str | None, settings: _impl.Settings) -> str | None:
    if value is None:
        return None
    redacted = _impl._redact_sensitive_text(str(value), settings) or ""
    for pattern in ARTIFACT_SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}{_impl.SECRET_REDACTION}", redacted)
    return redacted


def _safe_packet_text(value: Any, settings: _impl.Settings, *, default: str = "unknown") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return _impl._format_optional_bool(value)
    if isinstance(value, dict):
        width = value.get("width") or value.get("w")
        height = value.get("height") or value.get("h")
        value = f"{width}x{height}" if width and height else json.dumps(value, sort_keys=True)
    elif isinstance(value, list):
        value = "; ".join(str(item) for item in value) if value else "none"
    redacted = _redact_artifact_text(str(value), settings) or default
    return _truncate_packet_text(redacted)


def _safe_md_cell(value: Any, settings: _impl.Settings, *, default: str = "unknown") -> str:
    return _impl._md_cell(_safe_packet_text(value, settings, default=default))


def _retrieval_reference(job_id: str, file_name: str) -> str:
    return f"GET /api/v1/evidence/{quote(job_id, safe='')}/files/{quote(file_name, safe='')}"


def _truncate_packet_text(value: str, *, limit: int = 180) -> str:
    compact = " ".join(str(value).split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def _build_evidence_packet_section(result: _impl.HermesDispatchResult, settings: _impl.Settings) -> str:
    evidence = result.evidence
    if evidence is None:
        fallback = "\n".join(f"- {item}" for item in _impl.EVIDENCE_FILES)
        return f"### Evidence\n{fallback}\n\nEvidence manifest metadata was not fetched for this run.\n"
    lines = [
        "### Evidence Packet",
        f"- Hermes job ID: {_safe_packet_text(evidence.job_id, settings)}",
        f"- Manifest fetched: {evidence.manifest_fetched}",
        f"- Bundle fetched: {evidence.bundle_fetched}",
        f"- Bundle content type: {_safe_packet_text(evidence.bundle_content_type, settings)}",
        f"- Bundle size: {_impl._format_optional_int(evidence.bundle_size)}",
        f"- Page title: {_safe_packet_text(evidence.page_title, settings)}",
        f"- Final URL: {_safe_packet_text(evidence.final_url, settings)}",
        f"- HTTP status: {_impl._format_optional_int(evidence.http_status)}",
        f"- Viewport: {_safe_packet_text(_get_extra(evidence, 'viewport'), settings)}",
        f"- User agent: {_safe_packet_text(_get_extra(evidence, 'user_agent'), settings)}",
        f"- Load duration: {_safe_packet_text(_get_extra(evidence, 'load_duration'), settings)}",
        f"- Screenshot presence: {_impl._format_optional_bool(evidence.screenshot_present)}",
        f"- Console warning count: {_impl._format_optional_int(evidence.console_warning_count)}",
        f"- Console error count: {_impl._format_optional_int(evidence.console_error_count)}",
        f"- Console info count: {_safe_packet_text(_get_extra(evidence, 'console_info_count'), settings)}",
        f"- Console log count: {_safe_packet_text(_get_extra(evidence, 'console_log_count'), settings)}",
        f"- Console warning excerpts: {_safe_packet_text(_get_extra(evidence, 'console_warning_excerpts'), settings, default='none')}",
        f"- Console error excerpts: {_safe_packet_text(_get_extra(evidence, 'console_error_excerpts'), settings, default='none')}",
        f"- Network request count: {_safe_packet_text(_get_extra(evidence, 'network_request_count'), settings)}",
        f"- Network response count: {_safe_packet_text(_get_extra(evidence, 'network_response_count'), settings)}",
        f"- Network failure count: {_impl._format_optional_int(evidence.network_failure_count)}",
        f"- Network non-2xx count: {_impl._format_optional_int(evidence.network_non_2xx_count)}",
        f"- Network failed requests: {_safe_packet_text(_get_extra(evidence, 'network_failed_requests'), settings, default='none')}",
        f"- Network non-2xx requests: {_safe_packet_text(_get_extra(evidence, 'network_non_2xx_requests'), settings, default='none')}",
        "",
        "| File | Content type | Size | SHA256 | Retrieval |",
        "| --- | --- | ---: | --- | --- |",
    ]
    artifacts = evidence.artifacts or [_impl.HermesEvidenceArtifact(file_name=item) for item in _impl.EVIDENCE_FILES]
    for artifact in artifacts:
        retrieval = _retrieval_reference(evidence.job_id, artifact.file_name)
        lines.append("| " + " | ".join([
            _safe_md_cell(artifact.file_name, settings),
            _safe_md_cell(artifact.content_type, settings),
            _safe_md_cell(_impl._format_optional_int(artifact.size), settings),
            _safe_md_cell(artifact.sha256, settings),
            _safe_md_cell(retrieval, settings),
        ]) + " |")
    return "\n".join(lines) + "\n"


def _evidence_slack_status(result: _impl.HermesDispatchResult) -> str | None:
    if result.evidence and result.evidence.manifest_fetched:
        identifier = result.job_id or result.evidence.job_id
        return f"manifest fetched (GET /api/v1/evidence/{identifier}/manifest)" if identifier else "manifest fetched"
    if result.evidence is None:
        return None
    return "manifest not fetched"


def build_hermes_slack_message(parsed: _impl.ParsedGitHubEvent, result: _impl.HermesDispatchResult, settings: _impl.Settings) -> str:
    repo = _impl._sanitize_slack_text(parsed.repository or "unknown repo")
    subject_number = _impl._subject_number(parsed) or "unknown"
    subject_label = _impl._github_subject_label(parsed)
    labels = ", ".join(_impl._sanitize_slack_text(label) for label in parsed.labels) if parsed.labels else "none"
    target = _impl._sanitize_slack_text(_impl._redact_sensitive_text(result.target_url or settings.hermes_default_target, settings) or "unknown")
    if result.status == "BLOCKED":
        reason = _impl._sanitize_slack_text(_impl._redact_sensitive_text(result.error or result.skipped_reason or "Hermes validation could not run.", settings))
        return f"Hermes validation blocked\nReason: {reason}\nRepo: {repo}\n{subject_label}: #{subject_number}\nTarget: {target}\nNode: {result.hermes_node}\nCorrelation ID: {_impl._sanitize_slack_text(result.correlation_id or 'unknown')}"
    if result.status in {"PASSED", "FAILED"}:
        evidence_status = _evidence_slack_status(result)
        evidence_files = ", ".join(_impl.EVIDENCE_FILES)
        evidence_line = f"{evidence_status}; {evidence_files}" if evidence_status else evidence_files
        return f"Hermes validation complete\nRepo: {repo}\n{subject_label}: #{subject_number}\nTarget: {target}\nStatus: {result.status}\nJob ID: {_impl._sanitize_slack_text(result.job_id or 'unknown')}\nEvidence: {evidence_line}"
    return f"Hermes validation requested\nRepo: {repo}\n{subject_label}: #{subject_number}\nTarget: {target}\nLabels: {labels}\nNode: {result.hermes_node}\nCorrelation ID: {_impl._sanitize_slack_text(result.correlation_id or 'unknown')}"


_impl.HermesHTTPClient = HermesHTTPClient
_impl._result_from_hermes_response = _result_from_hermes_response
_impl._collect_hermes_evidence = _collect_hermes_evidence
_impl._build_evidence_packet_section = _build_evidence_packet_section
_impl.build_hermes_slack_message = build_hermes_slack_message

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

globals()["HermesHTTPClient"] = HermesHTTPClient

__all__ = [_name for _name in globals() if not _name.startswith("_")]
