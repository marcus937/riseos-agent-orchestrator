from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Any
from urllib.parse import quote

SECRET_REDACTION = "[REDACTED]"
DEFAULT_EVIDENCE_FILES = ["summary.json", "logs.json", "console.json", "network.json", "page.json", "screenshot.png"]
JSON_EVIDENCE_FILES = ["summary.json", "page.json", "console.json", "network.json", "logs.json"]
PASSWORD_PATTERN = re.compile(r"(?i)((?:password)\s*[:=]\s*['\"]?)([^'\"\s,;}]+)")


def install_hermes_evidence_patch(module: Any) -> None:
    if getattr(module, "_wf20_evidence_patch_installed", False):
        return

    original_redact = module._redact_sensitive_text
    original_notify = module._notify_and_writeback

    def redact_sensitive_text(value: str | None, settings: Any) -> str | None:
        redacted = original_redact(value, settings)
        if redacted is None:
            return None
        return PASSWORD_PATTERN.sub(lambda match: f"{match.group(1)}{SECRET_REDACTION}", redacted)

    def result_from_hermes_response(response: dict[str, Any], *, node: str, dispatch_key: str, correlation_id: str) -> Any:
        result = module._wf20_original_result_from_hermes_response(
            response,
            node=node,
            dispatch_key=dispatch_key,
            correlation_id=correlation_id,
        )
        if result.job_id is None:
            job_id = _deep_first(response, ("jobId", "job_id", "id"))
            if job_id is None:
                job_id = _deep_first(response.get("job") if isinstance(response, dict) else None, ("jobId", "job_id", "id"))
            if job_id is not None:
                result.job_id = str(job_id)
        return result

    async def notify_and_writeback(parsed: Any, settings: Any, result: Any, *, slack_client: Any | None, github_client: Any | None) -> Any:
        response = await original_notify(parsed, settings, result, slack_client=slack_client, github_client=github_client)
        _redact_result_fields(response, settings, module)
        return response

    async def collect_hermes_evidence(hermes_client: Any, base_url: str, token: str, job_id: str, settings: Any) -> Any | None:
        get_manifest = getattr(hermes_client, "get_evidence_manifest", None)
        get_bundle = getattr(hermes_client, "get_evidence_bundle", None)
        get_file = getattr(hermes_client, "get_evidence_file", None)
        if get_manifest is None and get_bundle is None and get_file is None:
            return None

        snapshot = module.HermesEvidenceSnapshot(job_id=job_id)
        errors: list[str] = []
        artifact_payloads: dict[str, Any] = {}
        bundle_files: dict[str, Any] = {}

        if get_manifest is not None:
            try:
                manifest = await get_manifest(base_url, token, job_id)
                snapshot.manifest_fetched = True
                snapshot.manifest = manifest if isinstance(manifest, dict) else {"raw": manifest}
                module._populate_evidence_from_manifest(snapshot)
            except Exception as exc:
                errors.append(f"manifest fetch failed: {module._redact_sensitive_text(str(exc), settings)}")

        if get_bundle is not None:
            try:
                bundle = await get_bundle(base_url, token, job_id)
                snapshot.bundle_fetched = True
                if isinstance(bundle, dict):
                    snapshot.bundle_content_type = module._first_string(bundle, "content_type", "contentType", "mimeType")
                    snapshot.bundle_size = module._first_int(bundle, "size", "contentLength", "content_length", "bytes")
                    bundle_files = _json_files_from_bundle(bundle)
                    artifact_payloads.update(bundle_files)
                    if _bundle_has_screenshot(bundle):
                        snapshot.screenshot_present = True
            except Exception as exc:
                errors.append(f"bundle fetch failed: {module._redact_sensitive_text(str(exc), settings)}")

        if get_file is not None:
            for file_name in JSON_EVIDENCE_FILES:
                if file_name in bundle_files:
                    continue
                try:
                    response = await get_file(base_url, token, job_id, file_name)
                    payload = _json_from_file_response(response)
                    if payload is not None:
                        artifact_payloads[file_name] = payload
                except Exception as exc:
                    errors.append(f"{file_name} fetch failed: {module._redact_sensitive_text(str(exc), settings)}")

        _hydrate_snapshot_from_payloads(snapshot, artifact_payloads)
        if errors:
            snapshot.error = "; ".join(error for error in errors if error)
        return snapshot

    def build_hermes_slack_message(parsed: Any, result: Any, settings: Any) -> str:
        repo = module._sanitize_slack_text(parsed.repository or "unknown repo")
        subject_number = module._subject_number(parsed) or "unknown"
        subject_label = module._github_subject_label(parsed)
        labels = ", ".join(module._sanitize_slack_text(label) for label in parsed.labels) if parsed.labels else "none"
        target = module._sanitize_slack_text(module._redact_sensitive_text(result.target_url or settings.hermes_default_target, settings) or "unknown")
        if result.status == "BLOCKED":
            reason = module._sanitize_slack_text(module._redact_sensitive_text(result.error or result.skipped_reason or "Hermes validation could not run.", settings))
            return f"Hermes validation blocked\nReason: {reason}\nRepo: {repo}\n{subject_label}: #{subject_number}\nTarget: {target}\nNode: {result.hermes_node}\nCorrelation ID: {module._sanitize_slack_text(result.correlation_id or 'unknown')}"
        if result.status in {"PASSED", "FAILED"}:
            return f"Hermes validation complete\nRepo: {repo}\n{subject_label}: #{subject_number}\nTarget: {target}\nStatus: {result.status}\nJob ID: {module._sanitize_slack_text(result.job_id or 'unknown')}\nEvidence: {', '.join(DEFAULT_EVIDENCE_FILES)}"
        return f"Hermes validation requested\nRepo: {repo}\n{subject_label}: #{subject_number}\nTarget: {target}\nLabels: {labels}\nNode: {result.hermes_node}\nCorrelation ID: {module._sanitize_slack_text(result.correlation_id or 'unknown')}"

    def build_evidence_packet_section(result: Any, settings: Any) -> str:
        evidence = result.evidence
        if evidence is None:
            fallback = "\n".join(f"- {item}" for item in DEFAULT_EVIDENCE_FILES)
            return f"### Evidence\n{fallback}\n\nEvidence manifest metadata was not fetched for this run.\n"

        extra = _extra(evidence)
        lines = [
            "### Evidence Packet",
            f"- Hermes job ID: {evidence.job_id}",
            f"- Manifest fetched: {evidence.manifest_fetched}",
            f"- Bundle fetched: {evidence.bundle_fetched}",
            f"- Bundle content type: {evidence.bundle_content_type or 'unknown'}",
            f"- Bundle size: {module._format_optional_int(evidence.bundle_size)}",
            f"- Page title: {module._redact_sensitive_text(evidence.page_title, settings) or 'unknown'}",
            f"- Final URL: {module._redact_sensitive_text(evidence.final_url, settings) or 'unknown'}",
            f"- HTTP status: {module._format_optional_int(evidence.http_status)}",
        ]
        if extra.get("viewport"):
            lines.append(f"- Viewport: {extra['viewport']}")
        if extra.get("user_agent"):
            lines.append(f"- User agent: {module._redact_sensitive_text(str(extra['user_agent']), settings)}")
        if extra.get("load_duration_ms") is not None:
            lines.append(f"- Load duration: {extra['load_duration_ms']}")
        lines.extend(
            [
                f"- Screenshot presence: {module._format_optional_bool(evidence.screenshot_present)}",
                f"- Console warning count: {module._format_optional_int(evidence.console_warning_count)}",
                f"- Console error count: {module._format_optional_int(evidence.console_error_count)}",
            ]
        )
        if extra.get("console_info_count") is not None:
            lines.append(f"- Console info count: {extra['console_info_count']}")
        if extra.get("console_warning_excerpts"):
            lines.append(f"- Console warning excerpts: {_redacted_join(extra['console_warning_excerpts'], settings, module)}")
        if extra.get("console_error_excerpts"):
            lines.append(f"- Console error excerpts: {_redacted_join(extra['console_error_excerpts'], settings, module)}")
        if extra.get("network_request_count") is not None:
            lines.append(f"- Network request count: {extra['network_request_count']}")
        lines.extend(
            [
                f"- Network failure count: {module._format_optional_int(evidence.network_failure_count)}",
                f"- Network non-2xx count: {module._format_optional_int(evidence.network_non_2xx_count)}",
            ]
        )
        if extra.get("network_failed_requests"):
            lines.append(f"- Network failed requests: {_redacted_join(extra['network_failed_requests'], settings, module)}")
        if extra.get("network_non_2xx_requests"):
            lines.append(f"- Network non-2xx requests: {_redacted_join(extra['network_non_2xx_requests'], settings, module)}")
        if evidence.error:
            lines.append(f"- Evidence retrieval notes: {module._redact_sensitive_text(evidence.error, settings)}")

        lines.extend(["", "| File | Content type | Size | SHA256 | Retrieval |", "| --- | --- | ---: | --- | --- |"])
        artifacts = evidence.artifacts or [module.HermesEvidenceArtifact(file_name=item, retrieval_note=f"GET /api/v1/evidence/{evidence.job_id}/files/{item}") for item in DEFAULT_EVIDENCE_FILES]
        for artifact in artifacts:
            display_name = module._redact_sensitive_text(artifact.file_name, settings) or artifact.file_name
            retrieval = _retrieval_note(evidence.job_id, artifact.file_name, settings, module)
            lines.append(
                "| "
                + " | ".join(
                    [
                        module._md_cell(display_name),
                        module._md_cell(artifact.content_type or "unknown"),
                        module._md_cell(module._format_optional_int(artifact.size)),
                        module._md_cell(module._redact_sensitive_text(artifact.sha256 or "unknown", settings) or "unknown"),
                        module._md_cell(retrieval),
                    ]
                )
                + " |"
            )
        return "\n".join(lines) + "\n"

    module._wf20_original_result_from_hermes_response = module._result_from_hermes_response
    module._redact_sensitive_text = redact_sensitive_text
    module._result_from_hermes_response = result_from_hermes_response
    module._notify_and_writeback = notify_and_writeback
    module._collect_hermes_evidence = collect_hermes_evidence
    module.build_hermes_slack_message = build_hermes_slack_message
    module._build_evidence_packet_section = build_evidence_packet_section
    module._wf20_evidence_patch_installed = True


def _json_files_from_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    content = bundle.get("content")
    if not isinstance(content, (bytes, bytearray)) or not content:
        return {}
    payloads: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(bytes(content))) as archive:
            for name in archive.namelist():
                basename = name.rsplit("/", 1)[-1]
                if basename == "screenshot.png":
                    continue
                if not basename.endswith(".json"):
                    continue
                with archive.open(name) as handle:
                    payloads[basename] = json.loads(handle.read().decode("utf-8"))
    except Exception:
        return payloads
    return payloads


def _bundle_has_screenshot(bundle: dict[str, Any]) -> bool:
    content = bundle.get("content")
    if not isinstance(content, (bytes, bytearray)) or not content:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(bytes(content))) as archive:
            return any(name.rsplit("/", 1)[-1].lower().endswith((".png", ".jpg", ".jpeg", ".webp")) for name in archive.namelist())
    except Exception:
        return False


def _json_from_file_response(response: Any) -> Any | None:
    if isinstance(response, dict) and "content" in response:
        content = response.get("content")
    else:
        content = response
    if isinstance(content, (bytes, bytearray)):
        content = content.decode("utf-8")
    if isinstance(content, str):
        return json.loads(content)
    if isinstance(content, (dict, list)):
        return content
    return None


def _hydrate_snapshot_from_payloads(snapshot: Any, payloads: dict[str, Any]) -> None:
    page = _payload(payloads, "page.json")
    console = _payload(payloads, "console.json")
    network = _payload(payloads, "network.json")
    logs = _payload(payloads, "logs.json")

    extra = _extra(snapshot)
    if isinstance(page, dict):
        snapshot.page_title = snapshot.page_title or _first(page, "title", "pageTitle", "page_title")
        snapshot.final_url = snapshot.final_url or _first(page, "finalUrl", "final_url", "url")
        snapshot.http_status = snapshot.http_status if snapshot.http_status is not None else _first_int(page, "httpStatus", "http_status", "statusCode", "status_code", "status")
        viewport = page.get("viewport")
        if isinstance(viewport, dict):
            width = _first_int(viewport, "width", "w")
            height = _first_int(viewport, "height", "h")
            if width and height:
                extra["viewport"] = f"{width}x{height}"
        extra["user_agent"] = extra.get("user_agent") or _first(page, "userAgent", "user_agent")
        extra["load_duration_ms"] = extra.get("load_duration_ms") if extra.get("load_duration_ms") is not None else _first_int(page, "loadDurationMs", "load_duration_ms")

    messages = [*_entries(console), *_entries(logs)]
    warning_messages = [_message_text(item) for item in messages if _entry_level(item) in {"warning", "warn"}]
    error_messages = [_message_text(item) for item in messages if _entry_level(item) == "error"]
    info_count = sum(1 for item in messages if _entry_level(item) == "info")
    log_count = sum(1 for item in messages if _entry_level(item) == "log")
    if snapshot.console_warning_count is None:
        snapshot.console_warning_count = len(warning_messages)
    if snapshot.console_error_count is None:
        snapshot.console_error_count = len(error_messages)
    extra["console_info_count"] = info_count
    extra["console_log_count"] = log_count
    extra["console_warning_excerpts"] = [item for item in warning_messages if item]
    extra["console_error_excerpts"] = [item for item in error_messages if item]

    requests = _entries(network)
    failed_requests: list[str] = []
    non_2xx_requests: list[str] = []
    for item in requests:
        if not isinstance(item, dict):
            continue
        url = _first(item, "url", "requestUrl", "request_url") or "unknown"
        error = _first(item, "error", "failure", "message")
        status = _first_int(item, "status", "statusCode", "status_code")
        if error:
            failed_requests.append(f"{url} {error}")
        if status is not None and (status < 200 or status >= 300):
            non_2xx_requests.append(f"{url} status={status}")
    if snapshot.network_failure_count is None:
        snapshot.network_failure_count = len(failed_requests)
    if snapshot.network_non_2xx_count is None:
        snapshot.network_non_2xx_count = len(non_2xx_requests)
    extra["network_request_count"] = len(requests)
    extra["network_failed_requests"] = failed_requests
    extra["network_non_2xx_requests"] = non_2xx_requests

    if snapshot.screenshot_present is None:
        snapshot.screenshot_present = any(artifact.file_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) or "screenshot" in artifact.file_name.lower() for artifact in snapshot.artifacts)


def _payload(payloads: dict[str, Any], name: str) -> Any:
    value = payloads.get(name)
    return value if isinstance(value, (dict, list)) else None


def _entries(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    for key in ("messages", "entries", "requests"):
        entries = value.get(key)
        if isinstance(entries, list):
            return entries
    return []


def _entry_level(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("level") or item.get("type") or "").lower()


def _message_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("text") or item.get("message") or "")


def _first(value: dict[str, Any], *keys: str) -> str | None:
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


def _extra(snapshot: Any) -> dict[str, Any]:
    manifest = snapshot.manifest if isinstance(snapshot.manifest, dict) else {}
    extra = manifest.setdefault("_hydrated", {})
    if not isinstance(extra, dict):
        extra = {}
        manifest["_hydrated"] = extra
    snapshot.manifest = manifest
    return extra


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


def _redacted_join(values: list[str], settings: Any, module: Any) -> str:
    return "; ".join(module._redact_sensitive_text(str(value), settings) or "" for value in values)


def _retrieval_note(job_id: str, file_name: str, settings: Any, module: Any) -> str:
    redacted = module._redact_sensitive_text(file_name, settings) or file_name
    return f"GET /api/v1/evidence/{job_id}/files/{quote(redacted, safe='[]')}"


def _redact_result_fields(result: Any, settings: Any, module: Any) -> None:
    for field in ("dispatch_key", "target_url", "preview_url", "error", "message", "comment"):
        value = getattr(result, field, None)
        if isinstance(value, str):
            setattr(result, field, module._redact_sensitive_text(value, settings))
    evidence = getattr(result, "evidence", None)
    if evidence is not None:
        for field in ("page_title", "final_url", "error"):
            value = getattr(evidence, field, None)
            if isinstance(value, str):
                setattr(evidence, field, module._redact_sensitive_text(value, settings))
