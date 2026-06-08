from __future__ import annotations

import json
from typing import Any

from app import hermes_dispatch_impl as _impl


async def _get_evidence_file(self: _impl.HermesHTTPClient, base_url: str, token: str, job_id: str, file_name: str) -> Any:
    response = await self._http_client.get(
        f"{base_url.rstrip('/')}/api/v1/evidence/{job_id}/files/{file_name}",
        headers={"X-Hermes-Token": token},
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type") or ""
    if "json" in content_type or file_name.endswith(".json"):
        try:
            data = response.json()
        except json.JSONDecodeError:
            data = json.loads(response.text)
        return data if isinstance(data, dict) else {"raw": data}
    return response.content


async def _collect_hermes_evidence(
    hermes_client: _impl.HermesDispatchHTTPClient,
    base_url: str,
    token: str,
    job_id: str,
    settings: _impl.Settings,
) -> _impl.HermesEvidenceSnapshot | None:
    get_manifest = getattr(hermes_client, "get_evidence_manifest", None)
    get_file = getattr(hermes_client, "get_evidence_file", None)
    get_bundle = getattr(hermes_client, "get_evidence_bundle", None)
    if get_manifest is None and get_file is None and get_bundle is None:
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

    if not snapshot.manifest_fetched and get_file is not None:
        try:
            manifest_file = await get_file(base_url, token, job_id, "manifest.json")
            snapshot.manifest_fetched = True
            snapshot.manifest = manifest_file if isinstance(manifest_file, dict) else {"raw": manifest_file}
            _impl._populate_evidence_from_manifest(snapshot)
            errors = [error for error in errors if not error.startswith("manifest fetch failed:")]
        except Exception as exc:
            errors.append(f"manifest file fetch failed: {_impl._redact_sensitive_text(str(exc), settings)}")

    if get_bundle is not None:
        try:
            bundle = await get_bundle(base_url, token, job_id)
            snapshot.bundle_fetched = True
            if isinstance(bundle, dict):
                snapshot.bundle_content_type = _impl._first_string(bundle, "content_type", "contentType", "mimeType")
                snapshot.bundle_size = _impl._first_int(bundle, "size", "contentLength", "content_length", "bytes")
        except Exception as exc:
            errors.append(f"bundle fetch failed: {_impl._redact_sensitive_text(str(exc), settings)}")

    if errors:
        snapshot.error = "; ".join(error for error in errors if error)
    return snapshot


def _evidence_slack_status(result: _impl.HermesDispatchResult) -> str:
    if result.evidence and result.evidence.manifest_fetched:
        identifier = result.job_id or result.evidence.job_id
        return f"manifest fetched ({identifier}/manifest.json)" if identifier else "manifest fetched"
    return "manifest unavailable"


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
        return f"Hermes validation complete\nRepo: {repo}\n{subject_label}: #{subject_number}\nTarget: {target}\nStatus: {result.status}\nJob ID: {_impl._sanitize_slack_text(result.job_id or 'unknown')}\nEvidence: {evidence_status}; {', '.join(_impl.EVIDENCE_FILES)}"
    return f"Hermes validation requested\nRepo: {repo}\n{subject_label}: #{subject_number}\nTarget: {target}\nLabels: {labels}\nNode: {result.hermes_node}\nCorrelation ID: {_impl._sanitize_slack_text(result.correlation_id or 'unknown')}"


_impl.HermesHTTPClient.get_evidence_file = _get_evidence_file  # type: ignore[attr-defined]
_impl._collect_hermes_evidence = _collect_hermes_evidence
_impl.build_hermes_slack_message = build_hermes_slack_message

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

__all__ = [_name for _name in globals() if not _name.startswith("_")]
