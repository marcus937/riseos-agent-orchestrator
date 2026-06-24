from __future__ import annotations

from typing import Any

from app import hermes_contract as contract_module
from app.circuit_runtime_validation import RuntimeValidationRequest
from app.config import Settings
from app.github_events import ParsedGitHubEvent
from app.wf20_runtime_validation import (
    VALIDATION_TYPE,
    _workflow_correlation_id,
    _workflow_id,
    frontend_validation_profile_for_repo,
    resolve_vercel_readiness,
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
    readiness, target_url, target_source, reason = await resolve_vercel_readiness(parsed, github_client)
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
