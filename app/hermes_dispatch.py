from __future__ import annotations

from typing import Any, Literal

from app import hermes_dispatch_impl as _impl


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
        except Exception as exc:
            errors.append(f"bundle fetch failed: {_impl._redact_sensitive_text(str(exc), settings)}")

    if errors:
        snapshot.error = "; ".join(error for error in errors if error)
    return snapshot


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


_impl._result_from_hermes_response = _result_from_hermes_response
_impl._collect_hermes_evidence = _collect_hermes_evidence
_impl.build_hermes_slack_message = build_hermes_slack_message

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

__all__ = [_name for _name in globals() if not _name.startswith("_")]
