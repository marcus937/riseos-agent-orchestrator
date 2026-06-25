from __future__ import annotations

from typing import Any

from app import hermes_contract as contract_module
from app.circuit_runtime_validation import RuntimeValidationRequest
from app.config import Settings
from app.github_events import ParsedGitHubEvent
from app.operational_logging import log_event
from app.wf20_deployment_resume_v2 import install_event_driven_wf20_runtime_validation
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

READY_DEPLOYMENT_STATES = {"success", "ready"}
FAILED_DEPLOYMENT_STATES = {"error", "failure", "failed", "inactive"}
PENDING_DEPLOYMENT_STATES = {"pending", "queued", "in_progress", "building"}

install_event_driven_wf20_runtime_validation()


def install_safe_wf20_request_builder() -> None:
    contract_module.runtime_validation_request_from_parsed = runtime_validation_request_from_parsed


async def runtime_validation_request_from_parsed(
    parsed: ParsedGitHubEvent,
    settings: Settings,
    *,
    github_client: Any | None = None,
) -> RuntimeValidationRequest:
    parsed = await hydrate_deployment_status_pr_context(parsed, github_client)
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
    object.__setattr__(request, "raw_github_event", parsed.raw)
    return request


async def hydrate_deployment_status_pr_context(parsed: ParsedGitHubEvent, github_client: Any | None) -> ParsedGitHubEvent:
    if not _is_deployment_status_payload(parsed.raw):
        return parsed
    if parsed.pull_request_number is not None or github_client is None or not parsed.repository or not parsed.head_sha:
        return parsed

    pulls = await _list_pull_requests_for_commit(github_client, parsed.repository, parsed.head_sha)
    selected = _select_pull_request_for_commit(pulls, parsed.head_sha, parsed.head_ref)
    if selected is None:
        log_event(
            "vercel_preview_pr_correlation_failed",
            **_decision_context(parsed),
            workflow_id=_workflow_id(parsed),
            deployment_id=_deployment_id(parsed.raw),
            deployment_status_id=_deployment_status_id(parsed.raw),
            candidate_pr_count=len(pulls),
            reason="No pull request matched the deployment commit SHA.",
        )
        return parsed

    head = selected.get("head") if isinstance(selected.get("head"), dict) else {}
    base = selected.get("base") if isinstance(selected.get("base"), dict) else {}
    labels = selected.get("labels") if isinstance(selected.get("labels"), list) else []
    hydrated = parsed.model_copy(
        update={
            "pull_request_number": selected.get("number"),
            "head_ref": head.get("ref") or parsed.head_ref,
            "head_sha": head.get("sha") or parsed.head_sha,
            "head_repo_full_name": _repo_full_name(head.get("repo")),
            "base_ref": base.get("ref") or parsed.base_ref,
            "base_repo_full_name": _repo_full_name(base.get("repo")),
            "labels": _label_names(labels) or parsed.labels,
        }
    )
    log_event(
        "vercel_preview_pr_correlation_resolved",
        **_decision_context(hydrated),
        workflow_id=_workflow_id(hydrated),
        deployment_id=_deployment_id(parsed.raw),
        deployment_status_id=_deployment_status_id(parsed.raw),
        candidate_pr_count=len(pulls),
    )
    return hydrated


async def resolve_verified_vercel_readiness(
    parsed: ParsedGitHubEvent,
    github_client: Any | None,
) -> tuple[VercelReadiness, str | None, str, str | None]:
    workflow_id = _workflow_id(parsed)
    context = _decision_context(parsed)
    payload_preview_url = contract_module.preview_url_from_payload(parsed.raw)
    candidates: list[dict[str, Any]] = []

    log_event(
        "vercel_preview_readiness_started",
        **context,
        workflow_id=workflow_id,
        deployment_id=_deployment_id(parsed.raw),
        deployment_status_id=_deployment_status_id(parsed.raw),
        payload_preview_url=payload_preview_url,
    )

    if payload_preview_url:
        candidates.append(
            _candidate(
                source="webhook_payload",
                item=parsed.raw,
                preview_url=payload_preview_url,
                state=_payload_state(parsed.raw),
                reason="Preview URL was present in the webhook payload.",
            )
        )

    if github_client is None or not parsed.repository or not parsed.head_sha:
        decision = _decide_from_candidates(parsed, candidates, fallback_reason="No Vercel deployment status was available to verify preview readiness.")
        _log_final_decision(parsed, decision, candidates)
        return decision

    statuses = await _safe_list(github_client, "list_commit_statuses", parsed.repository, parsed.head_sha)
    checks = await _safe_list(github_client, "list_check_runs_for_ref", parsed.repository, parsed.head_sha)
    deployments = await _safe_list(github_client, "list_deployments", parsed.repository, parsed.head_sha)

    for item in statuses:
        candidates.append(
            _candidate(
                source="commit_status",
                item=item,
                preview_url=contract_module.preview_url_from_payload(item) or payload_preview_url,
                state=str(item.get("state") or ""),
                reason="Commit status candidate for PR head SHA.",
            )
        )
    for item in checks:
        state = str(item.get("conclusion") or item.get("status") or "")
        candidates.append(
            _candidate(
                source="check_run",
                item=item,
                preview_url=contract_module.preview_url_from_payload(item) or payload_preview_url,
                state=state,
                reason="Check-run candidate for PR head SHA.",
            )
        )
    for deployment in deployments:
        deployment_id = deployment.get("id")
        statuses_for_deployment: list[dict[str, Any]] = []
        if deployment_id is not None:
            statuses_for_deployment = await _safe_list(github_client, "list_deployment_statuses", parsed.repository, deployment_id)
        if not statuses_for_deployment:
            candidates.append(
                _candidate(
                    source="deployment",
                    item=deployment,
                    preview_url=contract_module.preview_url_from_payload(deployment) or payload_preview_url,
                    state=str(deployment.get("state") or ""),
                    reason="Deployment candidate had no deployment_status records.",
                    deployment=deployment,
                )
            )
        for deployment_status in statuses_for_deployment:
            candidates.append(
                _candidate(
                    source="deployment_status",
                    item=deployment_status,
                    preview_url=contract_module.preview_url_from_payload(deployment_status)
                    or contract_module.preview_url_from_payload(deployment)
                    or payload_preview_url,
                    state=str(deployment_status.get("state") or ""),
                    reason="Deployment status candidate for PR head SHA deployment.",
                    deployment=deployment,
                )
            )

    for candidate in candidates:
        _log_candidate_decision(parsed, candidate)

    decision = _decide_from_candidates(
        parsed,
        candidates,
        fallback_reason="Timed out waiting for verified Vercel preview deployment readiness.",
    )
    _log_final_decision(parsed, decision, candidates)
    return decision


def _candidate(
    *,
    source: str,
    item: dict[str, Any],
    preview_url: str | None,
    state: str | None,
    reason: str,
    deployment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deployment = deployment or item if source == "deployment" else deployment
    normalized_state = str(state or "").lower()
    is_vercel = _looks_like_vercel_item(item) or (deployment is not None and _looks_like_vercel_item(deployment)) or bool(preview_url)
    ready = bool(preview_url and is_vercel and _candidate_successful(item, normalized_state))
    failed = bool(is_vercel and _candidate_failed(item, normalized_state))
    rejection_reason = None
    if not ready:
        if not is_vercel:
            rejection_reason = "candidate was not recognized as Vercel preview metadata"
        elif not preview_url:
            rejection_reason = "candidate did not include a usable Vercel preview URL"
        elif failed:
            rejection_reason = "candidate reported failed Vercel deployment state"
        elif normalized_state in PENDING_DEPLOYMENT_STATES:
            rejection_reason = "candidate reported pending Vercel deployment state"
        else:
            rejection_reason = "candidate was not in a verified Ready/success state"
    return {
        "source": source,
        "item": item,
        "deployment": deployment,
        "preview_url": preview_url,
        "state": normalized_state,
        "environment": item.get("environment") or (deployment or {}).get("environment"),
        "deployment_id": (deployment or item).get("id") if source in {"deployment", "deployment_status"} else None,
        "deployment_status_id": item.get("id") if source == "deployment_status" else None,
        "ready": ready,
        "failed": failed,
        "reason": reason,
        "rejection_reason": rejection_reason,
    }


def _candidate_successful(item: dict[str, Any], state: str) -> bool:
    if state in READY_DEPLOYMENT_STATES:
        return True
    return _github_item_successful(item)


def _candidate_failed(item: dict[str, Any], state: str) -> bool:
    if state in FAILED_DEPLOYMENT_STATES:
        return True
    return _github_item_failed(item)


def _decide_from_candidates(
    parsed: ParsedGitHubEvent,
    candidates: list[dict[str, Any]],
    *,
    fallback_reason: str,
) -> tuple[VercelReadiness, str | None, str, str | None]:
    for candidate in candidates:
        if candidate.get("ready") and candidate.get("preview_url"):
            return VercelReadiness.READY, str(candidate["preview_url"]), f"github_verified_{candidate['source']}_preview_url", None
    if any(candidate.get("failed") for candidate in candidates):
        return VercelReadiness.FAILED, None, "vercel_failed", "Vercel preview deployment failed."
    return VercelReadiness.TIMEOUT, None, "vercel_preview_pending", fallback_reason


def _log_candidate_decision(parsed: ParsedGitHubEvent, candidate: dict[str, Any]) -> None:
    log_event(
        "vercel_preview_candidate_evaluated",
        **_decision_context(parsed),
        workflow_id=_workflow_id(parsed),
        source=candidate.get("source"),
        deployment_id=candidate.get("deployment_id"),
        deployment_status_id=candidate.get("deployment_status_id"),
        environment=candidate.get("environment"),
        state=candidate.get("state"),
        preview_url=candidate.get("preview_url"),
        target_url_selected=candidate.get("preview_url") if candidate.get("ready") else None,
        accepted=bool(candidate.get("ready")),
        rejected=not bool(candidate.get("ready")),
        rejection_reason=candidate.get("rejection_reason"),
        candidate_reason=candidate.get("reason"),
    )


def _log_final_decision(
    parsed: ParsedGitHubEvent,
    decision: tuple[VercelReadiness, str | None, str, str | None],
    candidates: list[dict[str, Any]],
) -> None:
    readiness, target_url, target_source, reason = decision
    log_event(
        "vercel_preview_readiness_decision",
        **_decision_context(parsed),
        workflow_id=_workflow_id(parsed),
        readiness=readiness.value,
        target_url_selected=target_url,
        target_url_source=target_source,
        reason=reason,
        candidate_count=len(candidates),
        accepted_candidate_count=sum(1 for candidate in candidates if candidate.get("ready")),
        failed_candidate_count=sum(1 for candidate in candidates if candidate.get("failed")),
    )


def _decision_context(parsed: ParsedGitHubEvent) -> dict[str, Any]:
    return {
        "repository": parsed.repository,
        "repo": parsed.repository,
        "pr_number": parsed.pull_request_number,
        "branch": parsed.head_ref,
        "head_sha": parsed.head_sha,
        "commit_sha": parsed.head_sha,
    }


def _payload_state(value: dict[str, Any]) -> str | None:
    deployment_status = value.get("deployment_status") if isinstance(value, dict) else None
    if isinstance(deployment_status, dict) and deployment_status.get("state"):
        return str(deployment_status.get("state"))
    if value.get("state"):
        return str(value.get("state"))
    return None


async def _safe_list(client: Any, method_name: str, *args: Any) -> list[dict[str, Any]]:
    method = getattr(client, method_name, None)
    if method is None:
        return []
    try:
        raw = await method(*args)
    except Exception as exc:
        log_event("vercel_preview_source_query_failed", source=method_name, error=str(exc))
        return []
    return raw if isinstance(raw, list) else []


async def _list_pull_requests_for_commit(client: Any, repo: str, sha: str) -> list[dict[str, Any]]:
    method = getattr(client, "list_pull_requests_for_commit", None)
    if method is not None:
        return await _safe_list(client, "list_pull_requests_for_commit", repo, sha)
    request = getattr(client, "_request", None)
    if request is None:
        return []
    try:
        raw = await request("GET", f"/repos/{repo}/commits/{sha}/pulls", params={"per_page": 100})
    except Exception as exc:
        log_event("vercel_preview_source_query_failed", source="list_pull_requests_for_commit", error=str(exc))
        return []
    return raw if isinstance(raw, list) else []


def _select_pull_request_for_commit(pulls: list[dict[str, Any]], sha: str, branch: str | None) -> dict[str, Any] | None:
    open_pulls = [pull for pull in pulls if str(pull.get("state") or "open").lower() == "open"] or pulls
    for pull in open_pulls:
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        if head.get("sha") == sha:
            return pull
    if branch:
        for pull in open_pulls:
            head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
            if head.get("ref") == branch:
                return pull
    return open_pulls[0] if len(open_pulls) == 1 else None


def _is_deployment_status_payload(value: dict[str, Any]) -> bool:
    return isinstance(value.get("deployment_status"), dict) and isinstance(value.get("deployment"), dict)


def _deployment_id(value: dict[str, Any]) -> Any | None:
    deployment = value.get("deployment") if isinstance(value, dict) else None
    return deployment.get("id") if isinstance(deployment, dict) else None


def _deployment_status_id(value: dict[str, Any]) -> Any | None:
    deployment_status = value.get("deployment_status") if isinstance(value, dict) else None
    return deployment_status.get("id") if isinstance(deployment_status, dict) else None


def _repo_full_name(value: Any) -> str | None:
    return str(value.get("full_name")) if isinstance(value, dict) and value.get("full_name") else None


def _label_names(value: list[Any]) -> list[str]:
    return [str(item.get("name")) for item in value if isinstance(item, dict) and item.get("name")]
