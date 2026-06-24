from __future__ import annotations

from typing import Any

from app import hermes_contract as contract_module
from app.circuit_runtime_validation import RuntimeValidationRequest
from app.config import Settings
from app.github_events import ParsedGitHubEvent
from app.wf20_runtime_validation import (
    VALIDATION_TYPE,
    VercelReadiness,
    _github_item_failed,
    _github_item_successful,
    _looks_like_vercel_item,
    _workflow_correlation_id,
    _workflow_id,
    frontend_validation_profile_for_repo,
)


def install_safe_wf20_request_builder() -> None:
    contract_module.runtime_validation_request_from_parsed = runtime_validation_request_from_parsed


async def runtime_validation_request_from_parsed(
    parsed: ParsedGitHubEvent,
    settings: Settings,
    *,
    github_client: Any | None = None,
) -> RuntimeValidationRequest:
    profile = frontend_validation_profile_for_repo(parsed.repository, labels=parsed.labels)
    readiness, target_url, target_source, reason = await resolve_verified_vercel_readiness(parsed, github_client)
    request = RuntimeValidationRequest(
        repo=parsed.repository or "unknown",
        issue_number=parsed.issue_number,
        pr_number=parsed.pull_request_number,
        branch=parsed.head_ref or settings.work_branch,
        base_branch=parsed.base_ref,
        target_url=target_url,
        target_url_source=target_source,
        target_url_pending_reason=reason,
        validation_type=VALIDATION_TYPE,
        requested_by="orchestrator_wf20",
        correlation_id=_workflow_correlation_id(parsed),
        workflow_id=_workflow_id(parsed),
    )
    object.__setattr__(request, "validation_profile", profile.validation_profile)
    object.__setattr__(request, "commit_sha", parsed.head_sha)
    object.__setattr__(request, "vercel_readiness", readiness.value)
    return request


async def resolve_verified_vercel_readiness(
    parsed: ParsedGitHubEvent,
    github_client: Any | None,
) -> tuple[VercelReadiness, str | None, str, str | None]:
    payload_preview_url = contract_module.preview_url_from_payload(parsed.raw)
    if github_client is None or not parsed.repository or not parsed.head_sha:
        return (
            VercelReadiness.TIMEOUT,
            None,
            "vercel_timeout",
            "No Vercel deployment status was available to verify preview readiness.",
        )

    statuses: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    try:
        raw_statuses = await github_client.list_commit_statuses(parsed.repository, parsed.head_sha)
        statuses = raw_statuses if isinstance(raw_statuses, list) else []
    except Exception:
        statuses = []
    try:
        raw_checks = await github_client.list_check_runs_for_ref(parsed.repository, parsed.head_sha)
        checks = raw_checks if isinstance(raw_checks, list) else []
    except Exception:
        checks = []

    vercel_items = [item for item in [*statuses, *checks] if _looks_like_vercel_item(item)]
    if any(_github_item_failed(item) for item in vercel_items):
        return VercelReadiness.FAILED, None, "vercel_failed", "Vercel preview deployment failed."

    for item in vercel_items:
        preview_url = contract_module.preview_url_from_payload(item) or payload_preview_url
        if preview_url and _github_item_successful(item):
            return VercelReadiness.READY, preview_url, "github_verified_vercel_preview_url", None

    return (
        VercelReadiness.TIMEOUT,
        None,
        "vercel_timeout",
        "Timed out waiting for verified Vercel preview deployment readiness.",
    )
