from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel

from app.circuit_runtime_validation import RuntimeValidationRequest
from app.config import Settings
from app.github_events import GitHubEventType, ParsedGitHubEvent
from app.hermes_dispatch_impl import (
    BB2_BLOCK_LABEL,
    CIRCUIT_BASE_BRANCH,
    CIRCUIT_HERMES_PR_ACTIONS,
    CIRCUIT_WORK_BRANCH,
    HERMES_COMMANDS,
    HERMES_LIFECYCLE_LABELS,
    HERMES_RUNTIME_LABELS,
    PREVIEW_URL_FIELD_NAMES,
    TERMINAL_LABELS,
    URL_PATTERN,
)
from app.operational_logging import log_event


class HermesPreviewMetadataClient(Protocol):
    async def list_commit_statuses(self, repo_full_name: str, ref: str) -> Any: ...
    async def list_check_runs_for_ref(self, repo_full_name: str, ref: str) -> Any: ...


class HermesRuntimeTarget(BaseModel):
    target_url: str | None = None
    target_source: str
    preview_url: str | None = None
    pending_reason: str | None = None


def runtime_validation_required_for_parsed(parsed: ParsedGitHubEvent, settings: Settings, *, has_review_context: bool) -> bool:
    if not settings.enable_runtime_validation_review_bridge or not has_review_context:
        return False
    if parsed.event_type != GitHubEventType.PULL_REQUEST:
        return False
    return runtime_validation_route_reason(parsed) is not None


def runtime_validation_route_reason(parsed: ParsedGitHubEvent) -> str | None:
    explicit = _explicit_hermes_command(parsed.comment_body)
    if parsed.event_type == GitHubEventType.PULL_REQUEST:
        if parsed.action not in {"labeled", "unlabeled", *CIRCUIT_HERMES_PR_ACTIONS}:
            return None
        if _labels_request_hermes(parsed.labels, explicit=explicit):
            return f"pull_request_{parsed.action}"
        if parsed.action in CIRCUIT_HERMES_PR_ACTIONS and _is_circuit_pr(parsed):
            return f"pull_request_{parsed.action}_circuit_hermes"
    return None


async def runtime_validation_request_from_parsed(
    parsed: ParsedGitHubEvent,
    settings: Settings,
    *,
    github_client: HermesPreviewMetadataClient | None = None,
) -> RuntimeValidationRequest:
    target = await resolve_runtime_validation_target(parsed, settings, github_client=github_client)
    return RuntimeValidationRequest(
        repo=parsed.repository or "unknown",
        issue_number=parsed.issue_number,
        pr_number=parsed.pull_request_number,
        branch=parsed.head_ref or settings.work_branch,
        base_branch=parsed.base_ref,
        target_url=target.target_url,
        target_url_source=target.target_source,
        target_url_pending_reason=target.pending_reason,
        validation_type="playwright",
        requested_by="orchestrator_webhook",
        correlation_id=None,
    )


async def resolve_runtime_validation_target(
    parsed: ParsedGitHubEvent,
    settings: Settings,
    *,
    github_client: HermesPreviewMetadataClient | None = None,
) -> HermesRuntimeTarget:
    payload_preview_url = preview_url_from_payload(parsed.raw)
    if payload_preview_url:
        _log_target(parsed, payload_preview_url, "webhook_payload_preview_url")
        return HermesRuntimeTarget(target_url=payload_preview_url, target_source="webhook_payload_preview_url", preview_url=payload_preview_url)

    github_preview_url = await preview_url_from_github_commit_metadata(parsed, github_client)
    if github_preview_url:
        _log_target(parsed, github_preview_url, "github_commit_preview_url")
        return HermesRuntimeTarget(target_url=github_preview_url, target_source="github_commit_preview_url", preview_url=github_preview_url)

    if parsed.event_type == GitHubEventType.PULL_REQUEST:
        reason = "No successful Vercel preview deployment is available for this PR head SHA yet."
        _log_target(parsed, None, "vercel_preview_pending", fallback_reason=reason)
        return HermesRuntimeTarget(target_url=None, target_source="vercel_preview_pending", pending_reason=reason)

    _log_target(parsed, settings.hermes_default_target, "hermes_default_target")
    return HermesRuntimeTarget(target_url=settings.hermes_default_target, target_source="hermes_default_target")


async def preview_url_from_github_commit_metadata(parsed: ParsedGitHubEvent, github_client: HermesPreviewMetadataClient | None) -> str | None:
    if github_client is None or not parsed.repository or not parsed.head_sha:
        return None
    candidates: list[tuple[datetime | None, str]] = []
    for method_name in ("list_commit_statuses", "list_check_runs_for_ref"):
        method = getattr(github_client, method_name, None)
        if method is None:
            continue
        try:
            payload = await method(parsed.repository, parsed.head_sha)
        except Exception:
            continue
        candidates.extend(_successful_preview_candidates(payload))
    candidates.sort(key=lambda item: item[0] or datetime.min, reverse=True)
    return candidates[0][1] if candidates else None


def preview_url_from_payload(value: Any) -> str | None:
    for url in _candidate_preview_urls(value):
        if _is_vercel_preview_url(url):
            return url
    return None


def _successful_preview_candidates(value: Any) -> list[tuple[datetime | None, str]]:
    items = value if isinstance(value, list) else [value]
    candidates: list[tuple[datetime | None, str]] = []
    for item in items:
        if not isinstance(item, dict) or not _github_item_successful(item):
            continue
        preview_url = preview_url_from_payload(item)
        if preview_url:
            candidates.append((_github_item_timestamp(item), preview_url))
    return candidates


def _github_item_successful(item: dict[str, Any]) -> bool:
    state = str(item.get("state") or "").lower()
    status = str(item.get("status") or "").lower()
    conclusion = str(item.get("conclusion") or "").lower()
    if state:
        return state == "success"
    if status or conclusion:
        return status == "completed" and conclusion == "success"
    return False


def _github_item_timestamp(item: dict[str, Any]) -> datetime | None:
    for key in ("completed_at", "updated_at", "created_at", "started_at"):
        value = item.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
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
    return (
        parsed.event_type == GitHubEventType.PULL_REQUEST
        and parsed.repository is not None
        and parsed.head_repo_full_name == parsed.repository
        and parsed.base_repo_full_name == parsed.repository
        and parsed.head_ref == CIRCUIT_WORK_BRANCH
        and parsed.base_ref == CIRCUIT_BASE_BRANCH
    )


def _log_target(parsed: ParsedGitHubEvent, deployment_url: str | None, target_url_source: str, *, fallback_reason: str | None = None) -> None:
    log_event(
        "runtime_validation_target_resolved",
        repo=parsed.repository,
        pr_number=parsed.pull_request_number,
        branch=parsed.head_ref,
        deployment_url=deployment_url,
        target_url_source=target_url_source,
        fallback_reason=fallback_reason,
    )


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
