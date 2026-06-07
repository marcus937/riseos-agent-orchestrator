from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.correlation import branch_from_parsed, correlation_id_from_parsed
from app.github_events import GitHubEventType, ParsedGitHubEvent
from app.slack_issue_dispatch import SlackClient, SlackIssueDispatchClient, _sanitize_slack_text

HERMES_RUNTIME_LABELS = {"runtime-agent", "playwright", "evidence", "testing"}
HERMES_LIFECYCLE_LABELS = {"bb-review-needed", "agent-review", "agent-ready", "agent-next"}
CANONICAL_HERMES_TRIGGER_LABELS = ("runtime-agent", "playwright", "bb-review-needed")
CIRCUIT_HERMES_PR_ACTIONS = {"opened", "synchronize", "ready_for_review"}
CIRCUIT_WORK_BRANCH = "agent-integration"
CIRCUIT_BASE_BRANCH = "main"
HERMES_COMMANDS = {"/hermes validate", "run hermes validation", "needs hermes validation", "hermes validate", "runtime validation requested"}
TERMINAL_LABELS = {"wontfix", "duplicate", "invalid", "agent-blocked", "agent-merged"}
BB2_BLOCK_LABEL = "bb2-blocked"
DGX_LABELS = {"dgx", "runtime-agent", "evidence", "mission-control", "frontend", "playwright"}
EVIDENCE_FILES = ["summary.json", "logs.json", "console.json", "network.json", "page.json", "screenshot.png"]
PLACEHOLDER_TARGETS = {"https://example.com", "http://example.com"}
PREVIEW_URL_FIELD_NAMES = {"preview_url", "previewurl", "preview", "target_url", "targeturl", "environment_url", "environmenturl", "deployment_url", "deploymenturl", "details_url", "detailsurl"}
URL_PATTERN = re.compile(r"https?://[^\s\]>)\"'}]+")
SECRET_REDACTION = "[REDACTED]"
SECRET_PATTERNS = (
    re.compile(r"(?i)((?:x-hermes-token|authorization|api[_-]?key|access[_-]?token|token)\s*[:=]\s*['\"]?)([^'\"\s,;}]+)"),
    re.compile(r"(?i)(bearer\s+)([^'\"\s,;}]+)"),
)
LOGGER = logging.getLogger("riseos_agent_orchestrator")


class HermesWritebackClient(Protocol):
    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> Any: ...
    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> Any: ...


class HermesDispatchHTTPClient(Protocol):
    async def post_job(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class HermesDispatchRegistry(Protocol):
    def claim_hermes_dispatch(self, dispatch_key: str) -> bool: ...
    def mark_hermes_dispatch(self, dispatch_key: str) -> None: ...


class HermesEvidenceArtifact(BaseModel):
    file_name: str
    content_type: str | None = None
    size: int | None = None
    sha256: str | None = None
    download_url: str | None = None
    retrieval_note: str | None = None


class HermesEvidenceSnapshot(BaseModel):
    job_id: str
    manifest_fetched: bool = False
    bundle_fetched: bool = False
    bundle_content_type: str | None = None
    bundle_size: int | None = None
    manifest: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[HermesEvidenceArtifact] = Field(default_factory=list)
    page_title: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    screenshot_present: bool | None = None
    console_warning_count: int | None = None
    console_error_count: int | None = None
    network_failure_count: int | None = None
    network_non_2xx_count: int | None = None
    error: str | None = None


class HermesDispatchResult(BaseModel):
    attempted: bool = False
    success: bool = False
    status: Literal["PASSED", "FAILED", "BLOCKED", "SKIPPED"] = "SKIPPED"
    hermes_node: Literal["M2", "DGX"] = "M2"
    dispatch_key: str | None = None
    correlation_id: str | None = None
    target_url: str | None = None
    target_source: str | None = None
    preview_url: str | None = None
    skipped_reason: str | None = None
    error: str | None = None
    message: str | None = None
    comment: str | None = None
    label: str | None = None
    job_id: str | None = None
    evidence: HermesEvidenceSnapshot | None = None


class InMemoryHermesDispatchRegistry:
    def __init__(self) -> None:
        self._dispatch_keys: set[str] = set()
    def claim_hermes_dispatch(self, dispatch_key: str) -> bool:
        if dispatch_key in self._dispatch_keys:
            return False
        self._dispatch_keys.add(dispatch_key)
        return True
    def mark_hermes_dispatch(self, dispatch_key: str) -> None:
        self._dispatch_keys.add(dispatch_key)
    def reset(self) -> None:
        self._dispatch_keys.clear()


class HermesHTTPClient:
    def __init__(self) -> None:
        self._http_client = httpx.AsyncClient(timeout=30.0)
    async def post_job(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._http_client.post(f"{base_url.rstrip('/')}/api/v1/jobs", headers={"X-Hermes-Token": token}, json=payload)
        response.raise_for_status()
        if not response.content:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {"raw": data}
    async def get_evidence_manifest(self, base_url: str, token: str, job_id: str) -> dict[str, Any]:
        response = await self._http_client.get(f"{base_url.rstrip('/')}/api/v1/evidence/{job_id}/manifest", headers={"X-Hermes-Token": token})
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {"raw": data}
    async def get_evidence_bundle(self, base_url: str, token: str, job_id: str) -> dict[str, Any]:
        response = await self._http_client.get(f"{base_url.rstrip('/')}/api/v1/evidence/{job_id}/bundle", headers={"X-Hermes-Token": token})
        response.raise_for_status()
        return {"content_type": response.headers.get("content-type"), "size": len(response.content or b"")}
    async def aclose(self) -> None:
        await self._http_client.aclose()


hermes_dispatch_registry = InMemoryHermesDispatchRegistry()


async def dispatch_hermes_runtime_validation(parsed: ParsedGitHubEvent, settings: Settings, *, slack_client: SlackIssueDispatchClient | None = None, github_client: HermesWritebackClient | None = None, hermes_client: HermesDispatchHTTPClient | None = None, registry: HermesDispatchRegistry = hermes_dispatch_registry) -> HermesDispatchResult:
    explicit = _explicit_hermes_command(parsed.comment_body)
    route = _route_reason(parsed)
    node = _hermes_node(parsed.labels)
    _log_hermes_decision(parsed, settings, "hermes_route_evaluated", node=node, route=route, explicit_command=explicit, labels_request_hermes=_labels_request_hermes(parsed.labels, explicit=explicit), runtime_label_match=bool(set(parsed.labels) & HERMES_RUNTIME_LABELS), lifecycle_label_match=bool(set(parsed.labels) & HERMES_LIFECYCLE_LABELS), terminal_label_match=bool(set(parsed.labels) & TERMINAL_LABELS), bb2_blocked=BB2_BLOCK_LABEL in set(parsed.labels))
    if route is None:
        return HermesDispatchResult(hermes_node=node, skipped_reason="Event does not require Hermes runtime validation.")
    target_url, target_source = await _resolve_hermes_target_url(parsed, settings, github_client=github_client)
    preview_url = target_url if _is_vercel_preview_url(target_url) else None
    correlation_id = _hermes_correlation_id(parsed, node=node)
    dispatch_key = _dispatch_key(parsed, target_url, node=node)
    disabled = _dispatch_disabled(settings, node=node)
    missing_config = _missing_config(settings, node=node)
    target_error = _target_url_error(target_url)
    eligibility_blocker = _eligibility_blocker(dispatch_key=dispatch_key, disabled=disabled, missing_config=missing_config, target_error=target_error)
    _log_hermes_decision(parsed, settings, "hermes_dispatch_eligibility_evaluated", node=node, route=route, dispatch_key=dispatch_key, dispatch_key_available=dispatch_key is not None, dispatch_enabled=eligibility_blocker is None, disabled_reason=disabled, missing_config=missing_config, target_error=target_error, eligibility_blocker=eligibility_blocker, hermes_target=target_url, hermes_target_source=target_source, preview_url=preview_url)
    if dispatch_key is None:
        return HermesDispatchResult(hermes_node=node, correlation_id=correlation_id, target_url=target_url, target_source=target_source, preview_url=preview_url, skipped_reason="Hermes dispatch key could not be determined.")
    if disabled:
        return HermesDispatchResult(hermes_node=node, dispatch_key=dispatch_key, correlation_id=correlation_id, target_url=target_url, target_source=target_source, preview_url=preview_url, skipped_reason=disabled)
    if node == "DGX":
        result = HermesDispatchResult(attempted=True, success=False, status="BLOCKED", hermes_node=node, dispatch_key=dispatch_key, correlation_id=correlation_id, target_url=target_url, target_source=target_source, preview_url=preview_url, error="Hermes DGX dispatch is not supported yet.", label="agent-blocked")
        return await _notify_and_writeback(parsed, settings, result, slack_client=slack_client, github_client=github_client)
    if missing_config:
        result = HermesDispatchResult(attempted=True, success=False, status="BLOCKED", hermes_node=node, dispatch_key=dispatch_key, correlation_id=correlation_id, target_url=target_url, target_source=target_source, preview_url=preview_url, error=missing_config, label="agent-blocked")
        return await _notify_and_writeback(parsed, settings, result, slack_client=slack_client, github_client=github_client)
    if target_error:
        result = HermesDispatchResult(attempted=True, success=False, status="BLOCKED", hermes_node=node, dispatch_key=dispatch_key, correlation_id=correlation_id, target_url=target_url, target_source=target_source, preview_url=preview_url, error=target_error, label="agent-blocked")
        return await _notify_and_writeback(parsed, settings, result, slack_client=slack_client, github_client=github_client)
    if not registry.claim_hermes_dispatch(dispatch_key):
        return HermesDispatchResult(hermes_node=node, dispatch_key=dispatch_key, correlation_id=correlation_id, target_url=target_url, target_source=target_source, preview_url=preview_url, skipped_reason=f"Hermes validation was already dispatched for this {_duplicate_subject_label(parsed)} commit and target.")
    trigger_label_error = await _apply_canonical_hermes_trigger_labels(parsed, settings, route=route, github_client=github_client)
    owns_client = hermes_client is None
    hermes_client = hermes_client or HermesHTTPClient()
    payload = build_hermes_job_payload(parsed, settings, node=node, correlation_id=correlation_id, route=route, target_url=target_url, target_source=target_source)
    base_url = _node_base_url(settings, node)
    token = _node_token(settings, node)
    _log_hermes_decision(parsed, settings, "hermes_post_attempted", node=node, route=route, dispatch_key=dispatch_key, hermes_base_url=base_url, hermes_target=target_url, hermes_target_source=target_source, preview_url=preview_url, payload_correlation_id=payload["correlationId"], payload_type=payload["type"])
    try:
        response = await hermes_client.post_job(base_url, token, payload)
        result = _result_from_hermes_response(response, node=node, dispatch_key=dispatch_key, correlation_id=correlation_id)
        result.target_url = target_url
        result.target_source = target_source
        result.preview_url = preview_url
        if result.job_id and result.status in {"PASSED", "FAILED"}:
            result.evidence = await _collect_hermes_evidence(hermes_client, base_url, token, result.job_id, settings)
        _log_hermes_decision(parsed, settings, "hermes_post_completed", node=node, route=route, dispatch_key=dispatch_key, status=result.status, success=result.success, job_id=result.job_id, evidence_manifest_fetched=result.evidence.manifest_fetched if result.evidence else None, evidence_bundle_fetched=result.evidence.bundle_fetched if result.evidence else None, hermes_target=target_url, preview_url=preview_url)
        if trigger_label_error and not result.error:
            result.error = trigger_label_error
        if result.evidence and result.evidence.error and not result.error:
            result.error = result.evidence.error
        if parsed.event_type == GitHubEventType.ISSUES and result.status == "FAILED":
            result.label = "agent-blocked"
        registry.mark_hermes_dispatch(dispatch_key)
    except Exception as exc:
        error = _redact_sensitive_text(str(exc), settings)
        _log_hermes_decision(parsed, settings, "hermes_post_failed", node=node, route=route, dispatch_key=dispatch_key, error=error)
        result = HermesDispatchResult(attempted=True, success=False, status="BLOCKED", hermes_node=node, dispatch_key=dispatch_key, correlation_id=correlation_id, target_url=target_url, target_source=target_source, preview_url=preview_url, error=error, label="agent-blocked")
    finally:
        if owns_client and hasattr(hermes_client, "aclose"):
            await hermes_client.aclose()
    return await _notify_and_writeback(parsed, settings, result, slack_client=slack_client, github_client=github_client)


def build_hermes_job_payload(parsed: ParsedGitHubEvent, settings: Settings, *, node: Literal["M2", "DGX"] = "M2", correlation_id: str | None = None, route: str | None = None, target_url: str | None = None, target_source: str | None = None) -> dict[str, Any]:
    commit_sha = parsed.head_sha or "unknown"
    subject_kind = _subject_kind(parsed)
    subject_number = _subject_number(parsed)
    branch = branch_from_parsed(parsed) or settings.work_branch
    resolved_target_url = target_url or _preview_url_from_payload(parsed.raw) or settings.hermes_default_target
    preview_url = resolved_target_url if _is_vercel_preview_url(resolved_target_url) else None
    labels = set(parsed.labels)
    if _is_circuit_pr(parsed):
        labels.update(CANONICAL_HERMES_TRIGGER_LABELS)
    payload: dict[str, Any] = {"source": "riseos-agent-orchestrator", "repo": parsed.repository, "subjectType": subject_kind, "commitSha": commit_sha, "branch": branch, "targetUrl": resolved_target_url, "previewUrl": preview_url, "preview_url": preview_url, "validationType": "playwright", "validation_type": "playwright", "targetSource": target_source or ("vercel_preview" if preview_url else "hermes_default_target"), "screenshotName": f"{subject_kind}-{subject_number}-validation.png", "labels": sorted(labels), "hermesNode": node, "trigger": route}
    if subject_kind == "issue":
        payload["issueNumber"] = subject_number
    else:
        payload["prNumber"] = subject_number
        payload["pr_number"] = subject_number
    return {"type": "playwright", "dryRun": False, "targetUrl": resolved_target_url, "preview_url": preview_url, "validation_type": "playwright", "correlationId": correlation_id or _hermes_correlation_id(parsed, node=node), "payload": payload}


def build_hermes_slack_message(parsed: ParsedGitHubEvent, result: HermesDispatchResult, settings: Settings) -> str:
    repo = _sanitize_slack_text(parsed.repository or "unknown repo")
    subject_number = _subject_number(parsed) or "unknown"
    subject_label = _github_subject_label(parsed)
    labels = ", ".join(_sanitize_slack_text(label) for label in parsed.labels) if parsed.labels else "none"
    target = _sanitize_slack_text(_redact_sensitive_text(result.target_url or settings.hermes_default_target, settings) or "unknown")
    if result.status == "BLOCKED":
        reason = _sanitize_slack_text(_redact_sensitive_text(result.error or result.skipped_reason or "Hermes validation could not run.", settings))
        return f"Hermes validation blocked\nReason: {reason}\nRepo: {repo}\n{subject_label}: #{subject_number}\nTarget: {target}\nNode: {result.hermes_node}\nCorrelation ID: {_sanitize_slack_text(result.correlation_id or 'unknown')}"
    if result.status in {"PASSED", "FAILED"}:
        evidence_status = "manifest fetched" if result.evidence and result.evidence.manifest_fetched else "manifest unavailable"
        return f"Hermes validation complete\nRepo: {repo}\n{subject_label}: #{subject_number}\nTarget: {target}\nStatus: {result.status}\nJob ID: {_sanitize_slack_text(result.job_id or 'unknown')}\nEvidence: {evidence_status}; {', '.join(EVIDENCE_FILES)}"
    return f"Hermes validation requested\nRepo: {repo}\n{subject_label}: #{subject_number}\nTarget: {target}\nLabels: {labels}\nNode: {result.hermes_node}\nCorrelation ID: {_sanitize_slack_text(result.correlation_id or 'unknown')}"


def build_hermes_pr_comment(parsed: ParsedGitHubEvent, result: HermesDispatchResult, settings: Settings) -> str:
    commit_sha = parsed.head_sha or "unknown"
    target_url = _redact_sensitive_text(result.target_url or settings.hermes_default_target, settings)
    preview_url = _redact_sensitive_text(result.preview_url or "not-resolved", settings)
    verified = "Orchestrator detected that Hermes validation could not run." if result.status == "BLOCKED" else "Hermes dispatch routing completed and produced this validation status."
    return (
        "## Hermes Runtime Validation\n\n"
        f"Status: {result.status}\nHermes node: {result.hermes_node}\nRepository: {parsed.repository or 'unknown'}\n{_github_subject_label(parsed)}: #{_subject_number(parsed) or 'unknown'}\nTarget: {target_url}\nPreview URL: {preview_url}\nTarget source: {result.target_source or 'hermes_default_target'}\nJob ID: {result.job_id or 'not-created'}\nCorrelation ID: {result.correlation_id or 'unknown'}\nCommit: {commit_sha}\n\n"
        f"{_build_evidence_packet_section(result, settings)}\n"
        "### VERIFIED\n"
        f"- {verified}\n- This label is runtime evidence only and is not merge approval.\n\n"
        "### ASSUMED\n"
        "- Runtime target is the resolved PR preview URL when available, otherwise the configured Hermes default target.\n"
        "- Phase 1 keeps raw artifacts retrievable through the authenticated Hermes evidence API rather than uploading them to GitHub.\n\n"
        "### UNVERIFIED\n"
        f"- {_unverified_evidence_line(result, settings)}\n"
    )


def _build_evidence_packet_section(result: HermesDispatchResult, settings: Settings) -> str:
    evidence = result.evidence
    if evidence is None:
        fallback = "\n".join(f"- {item}" for item in EVIDENCE_FILES)
        return f"### Evidence\n{fallback}\n\nEvidence manifest metadata was not fetched for this run.\n"
    lines = ["### Evidence Packet", f"- Hermes job ID: {evidence.job_id}", f"- Manifest fetched: {evidence.manifest_fetched}", f"- Bundle fetched: {evidence.bundle_fetched}", f"- Bundle content type: {evidence.bundle_content_type or 'unknown'}", f"- Bundle size: {_format_optional_int(evidence.bundle_size)}", f"- Page title: {evidence.page_title or 'unknown'}", f"- Final URL: {_redact_sensitive_text(evidence.final_url, settings) or 'unknown'}", f"- HTTP status: {_format_optional_int(evidence.http_status)}", f"- Screenshot presence: {_format_optional_bool(evidence.screenshot_present)}", f"- Console warning count: {_format_optional_int(evidence.console_warning_count)}", f"- Console error count: {_format_optional_int(evidence.console_error_count)}", f"- Network failure count: {_format_optional_int(evidence.network_failure_count)}", f"- Network non-2xx count: {_format_optional_int(evidence.network_non_2xx_count)}", "", "| File | Content type | Size | SHA256 | Retrieval |", "| --- | --- | ---: | --- | --- |"]
    artifacts = evidence.artifacts or [HermesEvidenceArtifact(file_name=item, retrieval_note=f"GET /api/v1/evidence/{evidence.job_id}/files/{item}") for item in EVIDENCE_FILES]
    for artifact in artifacts:
        retrieval = artifact.download_url or artifact.retrieval_note or f"GET /api/v1/evidence/{evidence.job_id}/files/{artifact.file_name}"
        lines.append("| " + " | ".join([_md_cell(artifact.file_name), _md_cell(artifact.content_type or "unknown"), _md_cell(_format_optional_int(artifact.size)), _md_cell(artifact.sha256 or "unknown"), _md_cell(_redact_sensitive_text(retrieval, settings) or "Hermes API")]) + " |")
    return "\n".join(lines) + "\n"


async def _collect_hermes_evidence(hermes_client: HermesDispatchHTTPClient, base_url: str, token: str, job_id: str, settings: Settings) -> HermesEvidenceSnapshot | None:
    get_manifest = getattr(hermes_client, "get_evidence_manifest", None)
    get_bundle = getattr(hermes_client, "get_evidence_bundle", None)
    if get_manifest is None and get_bundle is None:
        return None
    snapshot = HermesEvidenceSnapshot(job_id=job_id)
    errors: list[str] = []
    if get_manifest is not None:
        try:
            manifest = await get_manifest(base_url, token, job_id)
            snapshot.manifest_fetched = True
            snapshot.manifest = manifest if isinstance(manifest, dict) else {"raw": manifest}
            _populate_evidence_from_manifest(snapshot)
        except Exception as exc:
            errors.append(f"manifest fetch failed: {_redact_sensitive_text(str(exc), settings)}")
    if get_bundle is not None:
        try:
            bundle = await get_bundle(base_url, token, job_id)
            snapshot.bundle_fetched = True
            if isinstance(bundle, dict):
                snapshot.bundle_content_type = _first_string(bundle, "content_type", "contentType", "mimeType")
                snapshot.bundle_size = _first_int(bundle, "size", "contentLength", "content_length", "bytes")
        except Exception as exc:
            errors.append(f"bundle fetch failed: {_redact_sensitive_text(str(exc), settings)}")
    if errors:
        snapshot.error = "; ".join(error for error in errors if error)
    return snapshot


def _populate_evidence_from_manifest(snapshot: HermesEvidenceSnapshot) -> None:
    manifest = snapshot.manifest
    snapshot.page_title = _deep_first_string(manifest, ("pageTitle", "page_title", "title"))
    snapshot.final_url = _deep_first_string(manifest, ("finalUrl", "final_url", "url"))
    snapshot.http_status = _deep_first_int(manifest, ("httpStatus", "http_status", "statusCode", "status_code", "status"))
    snapshot.console_warning_count = _deep_first_int(manifest, ("consoleWarningCount", "console_warning_count", "warnings", "warningCount"))
    snapshot.console_error_count = _deep_first_int(manifest, ("consoleErrorCount", "console_error_count", "errors", "errorCount"))
    snapshot.network_failure_count = _deep_first_int(manifest, ("networkFailureCount", "network_failure_count", "failedRequests", "failureCount"))
    snapshot.network_non_2xx_count = _deep_first_int(manifest, ("networkNon2xxCount", "network_non_2xx_count", "non2xxCount", "non_2xx_count"))
    snapshot.artifacts = _artifacts_from_manifest(manifest, snapshot.job_id)
    snapshot.screenshot_present = any(artifact.file_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) or "screenshot" in artifact.file_name.lower() or (artifact.content_type or "").startswith("image/") for artifact in snapshot.artifacts)


def _artifacts_from_manifest(manifest: dict[str, Any], job_id: str) -> list[HermesEvidenceArtifact]:
    raw_artifacts = manifest.get("artifacts") or manifest.get("files") or manifest.get("evidenceFiles") or []
    if isinstance(raw_artifacts, dict):
        raw_artifacts = [{"name": name, **value} if isinstance(value, dict) else {"name": name, "value": value} for name, value in raw_artifacts.items()]
    artifacts: list[HermesEvidenceArtifact] = []
    if isinstance(raw_artifacts, list):
        for item in raw_artifacts:
            if isinstance(item, str):
                artifacts.append(HermesEvidenceArtifact(file_name=item, retrieval_note=f"GET /api/v1/evidence/{job_id}/files/{item}"))
            elif isinstance(item, dict):
                file_name = _first_string(item, "fileName", "file_name", "name", "path")
                if file_name:
                    artifacts.append(HermesEvidenceArtifact(file_name=file_name, content_type=_first_string(item, "contentType", "content_type", "mimeType", "mime"), size=_first_int(item, "size", "bytes", "contentLength", "content_length"), sha256=_first_string(item, "sha256", "sha", "digest"), download_url=_first_string(item, "downloadUrl", "download_url", "url"), retrieval_note=_first_string(item, "retrievalNote", "retrieval_note") or f"GET /api/v1/evidence/{job_id}/files/{file_name}"))
    return artifacts


def _route_reason(parsed: ParsedGitHubEvent) -> str | None:
    explicit = _explicit_hermes_command(parsed.comment_body)
    if parsed.event_type == GitHubEventType.ISSUE_COMMENT:
        return "pr_comment_hermes_validate" if parsed.action == "created" and parsed.pull_request_number and explicit else None
    if parsed.event_type == GitHubEventType.ISSUES:
        return "issue_labeled_hermes_validate" if parsed.action == "labeled" and _labels_request_hermes(parsed.labels, explicit=explicit) else None
    if parsed.event_type == GitHubEventType.PULL_REQUEST:
        if parsed.action not in {"labeled", "unlabeled", *CIRCUIT_HERMES_PR_ACTIONS}:
            return None
        if _labels_request_hermes(parsed.labels, explicit=explicit):
            return f"pull_request_{parsed.action}"
        if parsed.action in CIRCUIT_HERMES_PR_ACTIONS and _is_circuit_pr(parsed):
            return f"pull_request_{parsed.action}_circuit_hermes"
    if parsed.event_type == GitHubEventType.PULL_REQUEST_REVIEW and parsed.action == "submitted" and _labels_request_hermes(parsed.labels, explicit=explicit):
        return "pull_request_review_submitted"
    return None


def _labels_request_hermes(labels: list[str], *, explicit: bool = False) -> bool:
    normalized = set(labels)
    if normalized & TERMINAL_LABELS and not explicit:
        return False
    if BB2_BLOCK_LABEL in normalized and not explicit:
        return False
    return bool(normalized & HERMES_RUNTIME_LABELS and normalized & HERMES_LIFECYCLE_LABELS)


def _explicit_hermes_command(body: str | None) -> bool:
    normalized = (body or "").lower()
    return any(command in normalized for command in HERMES_COMMANDS)


def _is_circuit_pr(parsed: ParsedGitHubEvent) -> bool:
    return parsed.event_type == GitHubEventType.PULL_REQUEST and parsed.repository is not None and parsed.head_repo_full_name == parsed.repository and parsed.base_repo_full_name == parsed.repository and parsed.head_ref == CIRCUIT_WORK_BRANCH and parsed.base_ref == CIRCUIT_BASE_BRANCH


def _missing_canonical_hermes_trigger_labels(labels: list[str]) -> list[str]:
    existing = set(labels)
    return [label for label in CANONICAL_HERMES_TRIGGER_LABELS if label not in existing]


async def _apply_canonical_hermes_trigger_labels(parsed: ParsedGitHubEvent, settings: Settings, *, route: str, github_client: HermesWritebackClient | None) -> str | None:
    if not settings.enable_github_writeback or github_client is None or not route.endswith("_circuit_hermes"):
        return None
    if not parsed.repository or not parsed.pull_request_number:
        return "Cannot apply Hermes trigger labels without repository and PR number."
    try:
        for label in _missing_canonical_hermes_trigger_labels(parsed.labels):
            await github_client.apply_label(parsed.repository, parsed.pull_request_number, label)
    except Exception as exc:
        return _redact_sensitive_text(f"Hermes trigger label writeback failed: {exc}", settings)
    return None


def _hermes_node(labels: list[str]) -> Literal["M2", "DGX"]:
    normalized = set(labels)
    return "DGX" if "dgx" in normalized and normalized & DGX_LABELS else "M2"


def _dispatch_disabled(settings: Settings, *, node: Literal["M2", "DGX"]) -> str | None:
    if node == "DGX" and not settings.hermes_dgx_enable_dispatch:
        return "HERMES_DGX_ENABLE_DISPATCH=false."
    if node == "M2" and not settings.hermes_m2_enable_dispatch:
        return "HERMES_M2_ENABLE_DISPATCH=false."
    return None


def _missing_config(settings: Settings, *, node: Literal["M2", "DGX"]) -> str | None:
    if node == "DGX" and (not settings.hermes_dgx_base_url or not settings.hermes_dgx_token):
        return "Missing HERMES_DGX_BASE_URL or HERMES_DGX_TOKEN."
    if node == "M2" and (not settings.hermes_m2_base_url or not settings.hermes_m2_token):
        return "Missing HERMES_M2_BASE_URL or HERMES_M2_TOKEN."
    return None


async def _resolve_hermes_target_url(parsed: ParsedGitHubEvent, settings: Settings, *, github_client: HermesWritebackClient | None) -> tuple[str, str]:
    payload_preview_url = _preview_url_from_payload(parsed.raw)
    if payload_preview_url:
        return payload_preview_url, "webhook_payload_preview_url"
    github_preview_url = await _preview_url_from_github_commit_metadata(parsed, github_client)
    if github_preview_url:
        return github_preview_url, "github_commit_preview_url"
    return settings.hermes_default_target, "hermes_default_target"


async def _preview_url_from_github_commit_metadata(parsed: ParsedGitHubEvent, github_client: HermesWritebackClient | None) -> str | None:
    if github_client is None or not parsed.repository or not parsed.head_sha:
        return None
    for method_name in ("list_commit_statuses", "list_check_runs_for_ref"):
        method = getattr(github_client, method_name, None)
        if method is not None:
            try:
                preview_url = _preview_url_from_payload(await method(parsed.repository, parsed.head_sha))
                if preview_url:
                    return preview_url
            except Exception:
                pass
    return None


def _preview_url_from_payload(value: Any) -> str | None:
    for url in _candidate_preview_urls(value):
        if _is_vercel_preview_url(url):
            return url
    return None


def _candidate_preview_urls(value: Any, *, key: str | None = None) -> list[str]:
    urls: list[str] = []
    normalized_key = _normalize_preview_key(key)
    if isinstance(value, str):
        candidates = URL_PATTERN.findall(value)
        if not candidates and normalized_key in PREVIEW_URL_FIELD_NAMES and value.startswith(("http://", "https://")):
            candidates = [value]
        urls.extend(_clean_candidate_url(candidate) for candidate in candidates)
    elif isinstance(value, list):
        for item in value:
            urls.extend(_candidate_preview_urls(item))
    elif isinstance(value, dict):
        preferred_items: list[tuple[str, Any]] = []
        fallback_items: list[tuple[str, Any]] = []
        for raw_key, raw_value in value.items():
            if _normalize_preview_key(str(raw_key)) in PREVIEW_URL_FIELD_NAMES:
                preferred_items.append((str(raw_key), raw_value))
            else:
                fallback_items.append((str(raw_key), raw_value))
        for raw_key, raw_value in [*preferred_items, *fallback_items]:
            urls.extend(_candidate_preview_urls(raw_value, key=raw_key))
    return [url for url in urls if url]


def _normalize_preview_key(key: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (key or "").lower())


def _clean_candidate_url(url: str) -> str:
    return url.rstrip(".,;:)]}'\"")


def _is_vercel_preview_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and (host == "vercel.app" or host.endswith(".vercel.app"))


def _target_url_error(target_url: str) -> str | None:
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "HERMES_DEFAULT_TARGET must be an absolute http or https URL."
    if target_url.rstrip("/") in PLACEHOLDER_TARGETS:
        return "HERMES_DEFAULT_TARGET is a placeholder and cannot produce runtime validation writeback."
    return None


def _node_base_url(settings: Settings, node: Literal["M2", "DGX"]) -> str:
    return settings.hermes_dgx_base_url if node == "DGX" else settings.hermes_m2_base_url  # type: ignore[return-value]


def _node_token(settings: Settings, node: Literal["M2", "DGX"]) -> str:
    return settings.hermes_dgx_token if node == "DGX" else settings.hermes_m2_token  # type: ignore[return-value]


def _subject_kind(parsed: ParsedGitHubEvent) -> Literal["issue", "pr"]:
    return "issue" if parsed.event_type == GitHubEventType.ISSUES else "pr"


def _subject_number(parsed: ParsedGitHubEvent) -> int | None:
    return parsed.issue_number if _subject_kind(parsed) == "issue" else parsed.pull_request_number


def _github_subject_label(parsed: ParsedGitHubEvent) -> str:
    return "Issue" if _subject_kind(parsed) == "issue" else "PR"


def _duplicate_subject_label(parsed: ParsedGitHubEvent) -> str:
    return "item" if _subject_kind(parsed) == "issue" else "PR"


def _dispatch_key(parsed: ParsedGitHubEvent, target_url: str, *, node: Literal["M2", "DGX"]) -> str | None:
    repo = parsed.repository
    subject_number = _subject_number(parsed)
    commit_sha = parsed.head_sha or "unknown"
    if not repo or not subject_number:
        return None
    return f"{node}:{repo}:{_subject_kind(parsed)}:{subject_number}:sha:{commit_sha}:target:{target_url}"


def _hermes_correlation_id(parsed: ParsedGitHubEvent, *, node: Literal["M2", "DGX"]) -> str:
    repo = (parsed.repository or "unknown/unknown").replace("/", "-")
    subject_number = _subject_number(parsed) or "unknown"
    short_sha = (parsed.head_sha or "unknown")[:7]
    return f"hermes-{node.lower()}-{repo}-{_subject_kind(parsed)}-{subject_number}-{short_sha}"


def _eligibility_blocker(*, dispatch_key: str | None, disabled: str | None, missing_config: str | None, target_error: str | None) -> str | None:
    return "dispatch_key_missing" if dispatch_key is None else disabled or missing_config or target_error


def _redact_sensitive_text(value: str | None, settings: Settings) -> str | None:
    if value is None:
        return None
    redacted = value
    for secret in _known_secret_values(settings):
        redacted = redacted.replace(secret, SECRET_REDACTION)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}{SECRET_REDACTION}", redacted)
    return redacted


def _redact_sensitive_value(value: Any, settings: Settings) -> Any:
    if isinstance(value, str):
        return _redact_sensitive_text(value, settings)
    if isinstance(value, list):
        return [_redact_sensitive_value(item, settings) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_value(item, settings) for item in value)
    if isinstance(value, dict):
        return {key: _redact_sensitive_value(item, settings) for key, item in value.items()}
    return value


def _known_secret_values(settings: Settings) -> list[str]:
    names = ("github_webhook_secret", "github_token", "github_app_private_key_path", "openai_api_key", "orchestrator_admin_token", "slack_webhook_url", "slack_bot_token", "hermes_token", "hermes_m2_token", "hermes_dgx_token")
    return [value for name in names if isinstance((value := getattr(settings, name, None)), str) and len(value) >= 4]


def _log_hermes_decision(parsed: ParsedGitHubEvent, settings: Settings, event: str, *, node: Literal["M2", "DGX"], route: str | None = None, **fields: Any) -> None:
    payload = {"event": event, "correlation_id": _hermes_correlation_id(parsed, node=node), "orchestrator_correlation_id": correlation_id_from_parsed(parsed), "github_event": str(parsed.event_type), "repo_full_name": parsed.repository, "action": parsed.action, "action_label": parsed.action_label, "commit_sha": parsed.head_sha, "issue_number": parsed.issue_number, "pr_number": parsed.pull_request_number, "labels": sorted(set(parsed.labels)), "hermes_node": node, "route": route, **fields}
    sanitized = _redact_sensitive_value(payload, settings)
    LOGGER.info(json.dumps({key: value for key, value in sanitized.items() if value is not None or key == "route"}, sort_keys=True, default=str))


def _result_from_hermes_response(response: dict[str, Any], *, node: Literal["M2", "DGX"], dispatch_key: str, correlation_id: str) -> HermesDispatchResult:
    status_value = str(response.get("status") or response.get("result") or "PASSED").upper()
    job_id = response.get("jobId") or response.get("job_id") or response.get("id")
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
    return HermesDispatchResult(attempted=True, success=success, status=status, hermes_node=node, dispatch_key=dispatch_key, correlation_id=correlation_id, label=label, job_id=str(job_id) if job_id else None)


async def _notify_and_writeback(parsed: ParsedGitHubEvent, settings: Settings, result: HermesDispatchResult, *, slack_client: SlackIssueDispatchClient | None, github_client: HermesWritebackClient | None) -> HermesDispatchResult:
    request_message = build_hermes_slack_message(parsed, HermesDispatchResult(hermes_node=result.hermes_node, correlation_id=result.correlation_id, target_url=result.target_url, target_source=result.target_source, preview_url=result.preview_url), settings)
    final_message = build_hermes_slack_message(parsed, result, settings)
    result.message = final_message
    owns_slack = slack_client is None
    webhook_url = settings.hermes_slack_webhook_url or settings.slack_webhook_url
    channel = settings.hermes_slack_channel or settings.slack_channel
    if webhook_url or settings.slack_bot_token:
        slack_client = slack_client or SlackClient(webhook_url=webhook_url, bot_token=settings.slack_bot_token)
        try:
            await slack_client.post_message(channel=channel, text=request_message)
            if final_message != request_message:
                await slack_client.post_message(channel=channel, text=final_message)
        except Exception as exc:
            result.error = result.error or _redact_sensitive_text(str(exc), settings)
        finally:
            if owns_slack and slack_client is not None and hasattr(slack_client, "aclose"):
                await slack_client.aclose()
    if github_client is not None and settings.enable_github_writeback and result.label:
        subject_number = _subject_number(parsed)
        if parsed.repository and subject_number:
            comment = build_hermes_pr_comment(parsed, result, settings)
            result.comment = comment
            try:
                await github_client.post_issue_comment(parsed.repository, subject_number, comment)
                await github_client.apply_label(parsed.repository, subject_number, result.label)
            except Exception as exc:
                result.error = result.error or _redact_sensitive_text(str(exc), settings)
    return result


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


def _format_optional_int(value: int | None) -> str:
    return str(value) if value is not None else "unknown"


def _format_optional_bool(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _unverified_evidence_line(result: HermesDispatchResult, settings: Settings) -> str:
    error = _redact_sensitive_text(result.error, settings)
    if error:
        return error
    if not result.job_id:
        return "Hermes did not return a job ID, so evidence artifacts could not be retrieved."
    if result.evidence is None:
        return "Evidence retrieval was skipped because the configured Hermes client does not expose evidence endpoints in this execution path."
    if not result.evidence.manifest_fetched:
        return "Evidence manifest could not be fetched from Hermes."
    if not result.evidence.bundle_fetched:
        return "Evidence bundle could not be fetched from Hermes."
    return "No additional unverified items reported by Hermes."
