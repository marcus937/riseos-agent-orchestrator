from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Callable, Literal
from urllib.parse import urlparse

from app import circuit_runtime_validation as runtime_module
from app import hermes_contract as contract_module
from app.circuit_runtime_validation import (
    RuntimeValidationBB2Packet,
    RuntimeValidationEvidenceSummary,
    RuntimeValidationHermesSummary,
    RuntimeValidationRequest,
    RuntimeValidationResult,
    RuntimeValidationStore,
)
from app.clients.agent_bus import AgentBusClient
from app.clients.github import GitHubClient
from app.config import Settings
from app.github_events import GitHubEventType, ParsedGitHubEvent
from app.operational_logging import log_event

FRONTEND_PROFILE_ENV = "RUNTIME_VALIDATION_FRONTEND_PROFILES"
DOCUMENTATION_ONLY_LABELS = {"documentation", "documentation-only", "docs", "docs-only"}
BACKEND_ONLY_LABELS = {"backend", "backend-only", "api-only"}
SUPPORTED_PR_ACTIONS = {"opened", "synchronize", "ready_for_review"}
VALIDATION_TYPE = "playwright"
GITHUB_STATUS_CONTEXT = "Hermes Playwright Validation"
DEFAULT_FRONTEND_PROFILES = {
    "jarvis-mission-control": "jmc_frontend_preview_v1",
    "marcus937/jarvis-mission-control": "jmc_frontend_preview_v1",
    "rise-marketing-os": "marketing_dashboard_preview_v1",
    "marcus937/rise-marketing-os": "marketing_dashboard_preview_v1",
    "rise-signal": "frontend_playwright",
    "marcus937/rise-signal": "frontend_playwright",
}


class VercelReadiness(StrEnum):
    READY = "VERCEL_READY"
    FAILED = "VERCEL_FAILED"
    TIMEOUT = "VERCEL_TIMEOUT"


class RuntimeValidationState(StrEnum):
    REQUESTED = "HERMES_VALIDATION_REQUESTED"
    RUNNING = "HERMES_VALIDATION_RUNNING"
    PLAYWRIGHT_EXECUTED = "PLAYWRIGHT_EXECUTED"
    PASSED = "HERMES_VALIDATION_PASSED"
    FAILED = "HERMES_VALIDATION_FAILED"
    BLOCKED = "HERMES_VALIDATION_BLOCKED"


class FrontendValidationProfile:
    def __init__(self, *, requires_runtime_validation: bool, validation_profile: str | None = None) -> None:
        self.requires_runtime_validation = requires_runtime_validation
        self.validation_profile = validation_profile


def install_wf20_runtime_validation() -> None:
    """Install WF20 Orchestrator integration without changing Agent Bus gates."""

    _install_agent_bus_runtime_methods()
    _install_github_status_method()
    contract_module.runtime_validation_required_for_parsed = runtime_validation_required_for_parsed
    contract_module.runtime_validation_route_reason = runtime_validation_route_reason
    contract_module.runtime_validation_request_from_parsed = runtime_validation_request_from_parsed
    runtime_module.runtime_validation_store = AgentBusRuntimeValidationStore()


def frontend_validation_profile_for_repo(
    repository: str | None,
    *,
    labels: list[str] | None = None,
) -> FrontendValidationProfile:
    if not repository:
        return FrontendValidationProfile(requires_runtime_validation=False)
    normalized_labels = {label.lower() for label in labels or []}
    if normalized_labels & DOCUMENTATION_ONLY_LABELS:
        return FrontendValidationProfile(requires_runtime_validation=False)
    if normalized_labels & BACKEND_ONLY_LABELS:
        return FrontendValidationProfile(requires_runtime_validation=False)
    profiles = _frontend_profile_config()
    normalized = repository.strip().lower()
    repo_name = normalized.rsplit("/", 1)[-1]
    profile = profiles.get(normalized) or profiles.get(repo_name)
    return FrontendValidationProfile(requires_runtime_validation=profile is not None, validation_profile=profile)


def runtime_validation_required_for_parsed(
    parsed: ParsedGitHubEvent,
    settings: Settings,
    *,
    has_review_context: bool,
) -> bool:
    if not settings.enable_runtime_validation_review_bridge:
        return False
    if parsed.event_type != GitHubEventType.PULL_REQUEST:
        return False
    if parsed.action not in SUPPORTED_PR_ACTIONS:
        return False
    profile = frontend_validation_profile_for_repo(parsed.repository, labels=parsed.labels)
    if profile.requires_runtime_validation:
        return True
    return has_review_context and _is_legacy_circuit_runtime_bridge_pr(parsed)


def runtime_validation_route_reason(parsed: ParsedGitHubEvent) -> str | None:
    if parsed.event_type != GitHubEventType.PULL_REQUEST:
        return None
    if parsed.action not in SUPPORTED_PR_ACTIONS:
        return None
    profile = frontend_validation_profile_for_repo(parsed.repository, labels=parsed.labels)
    if profile.requires_runtime_validation:
        return f"pull_request_{parsed.action}_frontend_runtime_validation"
    if _is_legacy_circuit_runtime_bridge_pr(parsed):
        return f"pull_request_{parsed.action}_circuit_runtime_validation"
    return None


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
    request.validation_profile = profile.validation_profile  # type: ignore[attr-defined]
    request.commit_sha = parsed.head_sha  # type: ignore[attr-defined]
    request.vercel_readiness = readiness.value  # type: ignore[attr-defined]
    return request


async def resolve_vercel_readiness(
    parsed: ParsedGitHubEvent,
    github_client: Any | None,
) -> tuple[VercelReadiness, str | None, str, str | None]:
    payload_preview_url = contract_module.preview_url_from_payload(parsed.raw)
    if payload_preview_url:
        return VercelReadiness.READY, payload_preview_url, "webhook_payload_preview_url", None

    if github_client is None or not parsed.repository or not parsed.head_sha:
        return VercelReadiness.TIMEOUT, None, "vercel_timeout", "No Vercel preview deployment metadata was available before timeout."

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

    for item in [*statuses, *checks]:
        preview_url = contract_module.preview_url_from_payload(item)
        if preview_url and _github_item_successful(item):
            return VercelReadiness.READY, preview_url, "github_commit_preview_url", None

    if any(_github_item_failed(item) for item in [*statuses, *checks] if _looks_like_vercel_item(item)):
        return VercelReadiness.FAILED, None, "vercel_failed", "Vercel preview deployment failed."

    return VercelReadiness.TIMEOUT, None, "vercel_timeout", "Timed out waiting for Vercel preview deployment readiness."


class AgentBusRuntimeValidationStore(RuntimeValidationStore):
    def __init__(
        self,
        hermes_client_factory: Callable[..., Any] = runtime_module.CircuitHermesClient,
        agent_bus_client_factory: Callable[[Settings], AgentBusClient] | None = None,
        github_client_factory: Callable[[Settings], Any] | None = None,
    ) -> None:
        super().__init__(hermes_client_factory=hermes_client_factory)
        self._agent_bus_client_factory = agent_bus_client_factory or _default_agent_bus_client
        self._github_client_factory = github_client_factory or _default_github_client

    async def trigger(self, request: RuntimeValidationRequest, settings: Settings) -> RuntimeValidationResult:
        profile = str(getattr(request, "validation_profile", None) or frontend_validation_profile_for_repo(request.repo).validation_profile or "frontend_playwright")
        commit_sha = getattr(request, "commit_sha", None)
        agent_bus_client = self._agent_bus_client_factory(settings) if settings.enable_agent_bus_dispatch else None
        github_client = self._github_client_factory(settings) if settings.enable_github_writeback else None
        try:
            if agent_bus_client is not None:
                request.work_item_id = await _ensure_agent_bus_work_item(agent_bus_client, request, settings, profile=profile, commit_sha=commit_sha)
                await _record_agent_bus_state(agent_bus_client, request, RuntimeValidationState.REQUESTED, profile=profile, commit_sha=commit_sha)

            if request.target_url is None and request.target_url_source in {"vercel_failed", "vercel_timeout", "vercel_preview_pending"}:
                result = _blocked_result_from_request(request, settings, profile=profile)
                if agent_bus_client is not None:
                    await _record_agent_bus_state(
                        agent_bus_client,
                        request,
                        RuntimeValidationState.BLOCKED,
                        profile=profile,
                        commit_sha=commit_sha,
                        result="blocked",
                        error=result.error,
                    )
                await _write_github_runtime_outcome(github_client, request, result, commit_sha=commit_sha)
                return result

            if agent_bus_client is not None:
                await _record_agent_bus_state(agent_bus_client, request, RuntimeValidationState.RUNNING, profile=profile, commit_sha=commit_sha)
            result = await super().trigger(request, settings)
            if agent_bus_client is not None:
                await _record_agent_bus_state(agent_bus_client, request, RuntimeValidationState.PLAYWRIGHT_EXECUTED, profile=profile, commit_sha=commit_sha, job_id=result.hermes.job_id)
                final_state, final_status = _final_state_from_result(result)
                await _record_agent_bus_state(
                    agent_bus_client,
                    request,
                    final_state,
                    profile=profile,
                    commit_sha=commit_sha,
                    job_id=result.hermes.job_id,
                    result=final_status,
                    runtime_result=result,
                )
                agent_bus_view = await agent_bus_client.get_runtime_validation(work_item_id=request.work_item_id)  # type: ignore[attr-defined]
                result.review_dispatch["agent_bus_runtime_validation"] = agent_bus_view
                if final_status == "passed" and not _agent_bus_view_passed(agent_bus_view):
                    result.status = "failed"
                    result.error = "Agent Bus did not confirm passed Hermes Playwright evidence."
                    result.bb2.review_status = "blocked"
            await _write_github_runtime_outcome(github_client, request, result, commit_sha=commit_sha)
            return result
        finally:
            if agent_bus_client is not None:
                await agent_bus_client.aclose()
            if github_client is not None and hasattr(github_client, "aclose"):
                await github_client.aclose()


def _install_agent_bus_runtime_methods() -> None:
    if getattr(AgentBusClient, "_wf20_runtime_methods_installed", False):
        return

    async def record_runtime_validation(self: AgentBusClient, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._base_url:
            from app.clients.agent_bus import MissingAgentBusBaseUrlError

            raise MissingAgentBusBaseUrlError("AGENT_BUS_BASE_URL is required for runtime validation.")
        response = await self._client.post(f"{self._base_url}/runtime-validations", headers=self._headers(), json=payload)
        from app.clients.agent_bus import _object_response

        return _object_response(response, "POST", "/runtime-validations")

    async def get_runtime_validation(self: AgentBusClient, *, work_item_id: str | None = None, repository: str | None = None, pr_number: int | None = None, branch: str | None = None, workflow_id: str | None = None) -> dict[str, Any]:
        if not self._base_url:
            from app.clients.agent_bus import MissingAgentBusBaseUrlError

            raise MissingAgentBusBaseUrlError("AGENT_BUS_BASE_URL is required for runtime validation.")
        params = {key: value for key, value in {"work_item_id": work_item_id, "repository": repository, "pr_number": pr_number, "branch": branch, "workflow_id": workflow_id}.items() if value is not None}
        response = await self._client.get(f"{self._base_url}/runtime-validations/latest", headers=self._headers(), params=params)
        from app.clients.agent_bus import _object_response

        return _object_response(response, "GET", "/runtime-validations/latest")

    AgentBusClient.record_runtime_validation = record_runtime_validation  # type: ignore[attr-defined]
    AgentBusClient.get_runtime_validation = get_runtime_validation  # type: ignore[attr-defined]
    AgentBusClient._wf20_runtime_methods_installed = True  # type: ignore[attr-defined]


def _install_github_status_method() -> None:
    if getattr(GitHubClient, "_wf20_commit_status_installed", False):
        return

    async def create_commit_status(self: GitHubClient, repo_full_name: str, sha: str, *, state: str, context: str, description: str, target_url: str | None = None) -> Any:
        self._require_value(repo_full_name, "repo_full_name")
        self._require_value(sha, "sha")
        payload = {"state": state, "context": context, "description": description}
        if target_url:
            payload["target_url"] = target_url
        return await self._request("POST", f"/repos/{repo_full_name}/statuses/{sha}", json=payload)

    GitHubClient.create_commit_status = create_commit_status  # type: ignore[attr-defined]
    GitHubClient._wf20_commit_status_installed = True  # type: ignore[attr-defined]


def _frontend_profile_config() -> dict[str, str]:
    raw = os.getenv(FRONTEND_PROFILE_ENV)
    if not raw:
        return dict(DEFAULT_FRONTEND_PROFILES)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}
        for item in raw.split(","):
            if ":" in item:
                repo, profile = item.split(":", 1)
                data[repo.strip()] = profile.strip()
    if not isinstance(data, dict):
        return dict(DEFAULT_FRONTEND_PROFILES)
    return {str(repo).strip().lower(): str(profile).strip() for repo, profile in data.items() if repo and profile}


def _default_agent_bus_client(settings: Settings) -> AgentBusClient:
    return AgentBusClient(base_url=settings.agent_bus_base_url, token=settings.agent_bus_token, timeout_seconds=settings.agent_bus_timeout_seconds)


def _default_github_client(settings: Settings) -> Any | None:
    if not settings.github_token:
        return None
    return GitHubClient(token=settings.github_token)


async def _ensure_agent_bus_work_item(agent_bus_client: AgentBusClient, request: RuntimeValidationRequest, settings: Settings, *, profile: str, commit_sha: str | None) -> str | None:
    if request.work_item_id:
        return request.work_item_id
    payload = {
        "title": f"Runtime validation for {request.repo} PR #{request.pr_number}",
        "repository": request.repo,
        "issue_number": request.issue_number,
        "pr_number": request.pr_number,
        "owner_agent": settings.agent_bus_owner_agent,
        "review_agent": settings.agent_bus_review_agent,
        "metadata": {
            "branch": request.branch,
            "base_branch": request.base_branch,
            "commit_sha": commit_sha,
            "requires_runtime_validation": True,
            "validation_profile": profile,
            "workflow_id": request.workflow_id,
        },
    }
    work_item = await agent_bus_client.create_work_item({key: value for key, value in payload.items() if value is not None})
    return str(work_item.get("work_item_id") or "") or None


async def _record_agent_bus_state(agent_bus_client: AgentBusClient, request: RuntimeValidationRequest, state: RuntimeValidationState, *, profile: str, commit_sha: str | None, job_id: str | None = None, result: Literal["passed", "failed", "blocked"] | None = None, runtime_result: RuntimeValidationResult | None = None, error: str | None = None) -> dict[str, Any] | None:
    if not request.work_item_id:
        return None
    evidence = runtime_result.evidence if runtime_result is not None else None
    hermes = runtime_result.hermes if runtime_result is not None else None
    payload: dict[str, Any] = {
        "work_item_id": request.work_item_id,
        "state": state.value,
        "actor": "hermes" if state not in {RuntimeValidationState.REQUESTED, RuntimeValidationState.BLOCKED} else "orchestrator",
        "job_id": job_id or (hermes.job_id if hermes else None),
        "workflow_id": request.workflow_id,
        "repository": request.repo,
        "pr_number": request.pr_number,
        "branch": request.branch,
        "commit_sha": commit_sha,
        "target_url": request.target_url,
        "validation_profile": profile,
        "final_url": evidence.final_url if evidence else None,
        "http_status": evidence.http_status if evidence else None,
        "console_summary": _console_summary(evidence),
        "network_summary": _network_summary(evidence),
        "screenshot_artifact": _screenshot_artifact(evidence),
        "artifact_hashes": _artifact_hashes(evidence),
        "result": result,
        "metadata": {
            "source": "orchestrator_wf20",
            "workflow_id": request.workflow_id,
            "validation_profile": profile,
            "vercel_readiness": getattr(request, "vercel_readiness", None),
            "target_url_source": request.target_url_source,
            "error": error or (runtime_result.error if runtime_result else None),
            "timestamps": {
                "created_at": runtime_result.created_at.isoformat() if runtime_result else datetime.now(UTC).isoformat(),
                "completed_at": runtime_result.completed_at.isoformat() if runtime_result and runtime_result.completed_at else None,
            },
        },
    }
    return await agent_bus_client.record_runtime_validation(_compact(payload))  # type: ignore[attr-defined]


def _blocked_result_from_request(request: RuntimeValidationRequest, settings: Settings, *, profile: str) -> RuntimeValidationResult:
    now = datetime.now(UTC)
    reason = request.target_url_pending_reason or "Vercel preview deployment did not become ready."
    return RuntimeValidationResult(
        validation_id=f"wf20-blocked-{(request.workflow_id or 'unknown').replace('/', '-')}",
        status="blocked",
        repo=request.repo,
        issue_number=request.issue_number,
        pr_number=request.pr_number,
        branch=request.branch,
        base_branch=request.base_branch,
        work_item_id=request.work_item_id,
        workflow_id=request.workflow_id,
        validation_type=request.validation_type,
        requested_by=request.requested_by,
        created_at=now,
        completed_at=now,
        correlation_id=request.correlation_id or f"runtime-validation-{request.pr_number or 'unknown'}",
        hermes=RuntimeValidationHermesSummary(target_url=None, target_source=request.target_url_source, status="BLOCKED", error=reason),
        evidence=RuntimeValidationEvidenceSummary(error=reason),
        bb2=RuntimeValidationBB2Packet(packet_created=True, review_requested=False, review_status="blocked", review_context={"validation_profile": profile, "workflow_id": request.workflow_id}),
        error=reason,
    )


def _final_state_from_result(result: RuntimeValidationResult) -> tuple[RuntimeValidationState, Literal["passed", "failed", "blocked"]]:
    if result.hermes.status == "PASSED" and result.status == "completed":
        return RuntimeValidationState.PASSED, "passed"
    if result.hermes.status == "FAILED":
        return RuntimeValidationState.FAILED, "failed"
    return RuntimeValidationState.BLOCKED, "blocked"


def _agent_bus_view_passed(view: dict[str, Any]) -> bool:
    if view.get("current_state") != RuntimeValidationState.PASSED.value:
        return False
    history = view.get("history") or []
    latest = history[-1] if history else {}
    metadata = latest.get("metadata") if isinstance(latest, dict) else {}
    return isinstance(metadata, dict) and metadata.get("status") in {None, "passed"}


async def _write_github_runtime_outcome(github_client: Any | None, request: RuntimeValidationRequest, result: RuntimeValidationResult, *, commit_sha: str | None) -> None:
    if github_client is None or not request.repo or not request.pr_number:
        return
    status = _github_runtime_status(result)
    label = _github_runtime_label(result)
    comment = _github_runtime_comment(request, result)
    try:
        await github_client.post_issue_comment(request.repo, request.pr_number, comment)
        await github_client.apply_label(request.repo, request.pr_number, label)
        if commit_sha and hasattr(github_client, "create_commit_status"):
            await github_client.create_commit_status(
                request.repo,
                commit_sha,
                state=status,
                context=GITHUB_STATUS_CONTEXT,
                description=f"Hermes runtime validation {result.hermes.status}",
                target_url=result.hermes.target_url or request.target_url,
            )
    except Exception as exc:
        result.error = result.error or str(exc)


def _github_runtime_status(result: RuntimeValidationResult) -> str:
    return "success" if result.hermes.status == "PASSED" and result.status == "completed" else "failure"


def _github_runtime_label(result: RuntimeValidationResult) -> str:
    if result.hermes.status == "PASSED" and result.status == "completed":
        return "agent-verified"
    if result.hermes.status == "FAILED":
        return "agent-revisions"
    return "agent-blocked"


def _github_runtime_comment(request: RuntimeValidationRequest, result: RuntimeValidationResult) -> str:
    return "\n".join(
        [
            "## Hermes Runtime Validation",
            "",
            f"Status: {result.hermes.status}",
            f"Result: {result.status}",
            f"Repository: {request.repo}",
            f"PR: #{request.pr_number or 'unknown'}",
            f"Branch: {request.branch or 'unknown'}",
            f"Validation profile: {getattr(request, 'validation_profile', None) or 'frontend_playwright'}",
            f"Target URL: {result.hermes.target_url or request.target_url or 'not-ready'}",
            f"Final URL: {result.evidence.final_url or 'unknown'}",
            f"HTTP status: {result.evidence.http_status if result.evidence.http_status is not None else 'unknown'}",
            f"Job ID: {result.hermes.job_id or 'not-created'}",
            f"Workflow ID: {request.workflow_id or 'unknown'}",
            "",
            "### VERIFIED",
            "- Orchestrator reported runtime validation state to Agent Bus.",
            "- BB2 review remains gated by Agent Bus runtime validation eligibility.",
            "",
            "### ASSUMED",
            "- Vercel deployment metadata came from GitHub commit statuses/check runs or the webhook payload.",
            "",
            "### UNVERIFIED",
            f"- {result.error or 'No additional runtime validation gaps reported.'}",
        ]
    )


def _github_item_successful(item: dict[str, Any]) -> bool:
    state = str(item.get("state") or "").lower()
    status = str(item.get("status") or "").lower()
    conclusion = str(item.get("conclusion") or "").lower()
    return state == "success" or (status == "completed" and conclusion == "success")


def _github_item_failed(item: dict[str, Any]) -> bool:
    state = str(item.get("state") or "").lower()
    conclusion = str(item.get("conclusion") or "").lower()
    return state in {"failure", "error"} or conclusion in {"failure", "cancelled", "timed_out", "startup_failure"}


def _looks_like_vercel_item(item: dict[str, Any]) -> bool:
    haystack = " ".join(str(item.get(key) or "") for key in ("context", "name", "description", "target_url", "details_url", "html_url")).lower()
    return "vercel" in haystack or "deployment" in haystack or "preview" in haystack


def _is_legacy_circuit_runtime_bridge_pr(parsed: ParsedGitHubEvent) -> bool:
    normalized_labels = {label.lower() for label in parsed.labels or []}
    if normalized_labels & (DOCUMENTATION_ONLY_LABELS | BACKEND_ONLY_LABELS):
        return False
    return parsed.head_ref == "agent-integration" and parsed.base_ref == "main"


def _workflow_id(parsed: ParsedGitHubEvent) -> str:
    repo = (parsed.repository or "unknown").replace("/", "-")
    pr = parsed.pull_request_number or parsed.issue_number or "unknown"
    sha = (parsed.head_sha or "unknown")[:12]
    return f"wf20-{repo}-pr-{pr}-{sha}"


def _workflow_correlation_id(parsed: ParsedGitHubEvent) -> str:
    return _workflow_id(parsed)


def _console_summary(evidence: RuntimeValidationEvidenceSummary | None) -> dict[str, Any]:
    if evidence is None:
        return {}
    return _compact({"warnings": evidence.console_warning_count, "errors": evidence.console_error_count, "info": evidence.console_info_count, "logs": evidence.console_log_count})


def _network_summary(evidence: RuntimeValidationEvidenceSummary | None) -> dict[str, Any]:
    if evidence is None:
        return {}
    return _compact({"requests": evidence.network_request_count, "responses": evidence.network_response_count, "failures": evidence.network_failure_count, "non_2xx": evidence.network_non_2xx_count})


def _screenshot_artifact(evidence: RuntimeValidationEvidenceSummary | None) -> str | None:
    if evidence is None:
        return None
    for artifact in evidence.artifacts:
        name = str(artifact.get("file_name") or "")
        if "screenshot" in name.lower() or name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            return name
    return None


def _artifact_hashes(evidence: RuntimeValidationEvidenceSummary | None) -> dict[str, str]:
    if evidence is None:
        return {}
    hashes: dict[str, str] = {}
    for artifact in evidence.artifacts:
        name = artifact.get("file_name")
        digest = artifact.get("sha256")
        if name and digest:
            hashes[str(name)] = str(digest)
    return hashes


def _compact(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, {}, [])}


def _is_vercel_preview_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and (host == "vercel.app" or host.endswith(".vercel.app"))
