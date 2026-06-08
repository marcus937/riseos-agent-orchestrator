from __future__ import annotations

import io
import json
import zipfile
from pathlib import PurePosixPath
from typing import Any, Literal

from app import hermes_dispatch_impl as _impl

ARTIFACT_JSON_FILES = {"summary.json", "page.json", "console.json", "network.json", "logs.json"}


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
    if get_manifest is None and get_bundle is None:
        return None

    snapshot = _impl.HermesEvidenceSnapshot(job_id=job_id)
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
                _hydrate_snapshot_from_bundle(snapshot, bundle)
        except Exception as exc:
            errors.append(f"bundle fetch failed: {_impl._redact_sensitive_text(str(exc), settings)}")

    if errors:
        snapshot.error = "; ".join(error for error in errors if error)
    return snapshot


def _hydrate_snapshot_from_bundle(snapshot: _impl.HermesEvidenceSnapshot, bundle: dict[str, Any]) -> None:
    content = _bundle_content(bundle)
    if not content:
        return

    artifact_jsons, artifact_entries = _parse_bundle_artifacts(content)
    if artifact_entries:
        _merge_bundle_artifact_inventory(snapshot, artifact_entries)
    if artifact_jsons:
        _populate_evidence_from_artifact_jsons(snapshot, artifact_jsons)


def _bundle_content(bundle: dict[str, Any]) -> bytes | None:
    for key in ("content", "body", "data", "raw", "bytes"):
        value = bundle.get(key)
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
    return None


def _parse_bundle_artifacts(content: bytes) -> tuple[dict[str, Any], list[tuple[str, int]]]:
    artifact_jsons: dict[str, Any] = {}
    artifact_entries: list[tuple[str, int]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                file_name = PurePosixPath(info.filename).name
                artifact_entries.append((file_name, info.file_size))
                if file_name not in ARTIFACT_JSON_FILES:
                    continue
                try:
                    artifact_jsons[file_name] = json.loads(archive.read(info).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
    except zipfile.BadZipFile:
        try:
            artifact_jsons["summary.json"] = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return artifact_jsons, artifact_entries


def _merge_bundle_artifact_inventory(snapshot: _impl.HermesEvidenceSnapshot, artifact_entries: list[tuple[str, int]]) -> None:
    existing = {artifact.file_name for artifact in snapshot.artifacts}
    for file_name, size in artifact_entries:
        if file_name in existing:
            continue
        snapshot.artifacts.append(
            _impl.HermesEvidenceArtifact(
                file_name=file_name,
                content_type=_content_type_for_artifact(file_name),
                size=size,
                retrieval_note=f"GET /api/v1/evidence/{snapshot.job_id}/files/{file_name}",
            )
        )
    if any(_is_screenshot_artifact(file_name) for file_name, _size in artifact_entries):
        snapshot.screenshot_present = True


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

    page_sources = [source for source in (page, summary) if source is not None]
    snapshot.page_title = snapshot.page_title or _first_deep_string(page_sources, ("pageTitle", "page_title", "title"))
    snapshot.final_url = snapshot.final_url or _first_deep_string(page_sources, ("finalUrl", "final_url", "url"))
    snapshot.http_status = snapshot.http_status if snapshot.http_status is not None else _first_deep_int(page_sources, ("httpStatus", "http_status", "statusCode", "status_code", "status"))

    console_sources = [console, logs, summary]
    snapshot.console_warning_count = _coalesce_int(
        snapshot.console_warning_count,
        _first_deep_int(console_sources, ("consoleWarningCount", "console_warning_count", "warningCount", "warnings")),
        _count_console_entries(console, {"warning", "warn"}),
        _count_console_entries(logs, {"warning", "warn"}),
    )
    snapshot.console_error_count = _coalesce_int(
        snapshot.console_error_count,
        _first_deep_int(console_sources, ("consoleErrorCount", "console_error_count", "errorCount", "errors")),
        _count_console_entries(console, {"error"}),
        _count_console_entries(logs, {"error"}),
    )
    snapshot.network_failure_count = _coalesce_int(
        snapshot.network_failure_count,
        _first_deep_int([network, summary], ("networkFailureCount", "network_failure_count", "failedRequests", "failureCount", "failures")),
        _count_network_failures(network),
    )
    snapshot.network_non_2xx_count = _coalesce_int(
        snapshot.network_non_2xx_count,
        _first_deep_int([network, summary], ("networkNon2xxCount", "network_non_2xx_count", "non2xxCount", "non_2xx_count")),
        _count_network_non_2xx(network),
    )


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


def _coalesce_int(*values: int | None) -> int | None:
    for value in values:
        if value is not None:
            return value
    return None


def _count_console_entries(value: Any, levels: set[str]) -> int | None:
    entries = _as_list(value, "messages", "entries", "console", "logs")
    if entries is None:
        return None
    count = 0
    for item in entries:
        if isinstance(item, dict):
            level = str(item.get("level") or item.get("type") or "").lower()
            if level in levels:
                count += 1
    return count


def _count_network_failures(value: Any) -> int | None:
    entries = _as_list(value, "requests", "entries", "network")
    if entries is None:
        return None
    count = 0
    for item in entries:
        if not isinstance(item, dict):
            continue
        failed = item.get("failed") or item.get("failure") or item.get("error") or item.get("failureText")
        if failed:
            count += 1
    return count


def _count_network_non_2xx(value: Any) -> int | None:
    entries = _as_list(value, "requests", "entries", "network")
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


def _as_list(value: Any, *keys: str) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if isinstance(item, list):
                return item
    return None


def _evidence_slack_status(result: _impl.HermesDispatchResult) -> str | None:
    if result.evidence and result.evidence.manifest_fetched:
        identifier = result.job_id or result.evidence.job_id
        return f"manifest fetched (GET /api/v1/evidence/{identifier}/manifest)" if identifier else "manifest fetched"
    if result.evidence is None:
        return None
    return "manifest not fetched"


def build_hermes_slack_message(
    parsed: _impl.ParsedGitHubEvent,
    result: _impl.HermesDispatchResult,
    settings: _impl.Settings,
) -> str:
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
_impl.build_hermes_slack_message = build_hermes_slack_message

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

globals()["HermesHTTPClient"] = HermesHTTPClient

__all__ = [_name for _name in globals() if not _name.startswith("_")]
