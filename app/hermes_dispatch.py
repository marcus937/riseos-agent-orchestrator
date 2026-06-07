from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

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
HERMES_COMMANDS = {
    "/hermes validate",
    "run hermes validation",
    "needs hermes validation",
    "hermes validate",
    "runtime validation requested",
}
TERMINAL_LABELS = {"wontfix", "duplicate", "invalid", "agent-blocked", "agent-merged"}
BB2_BLOCK_LABEL = "bb2-blocked"
DGX_LABELS = {"dgx", "runtime-agent", "evidence", "mission-control", "frontend", "playwright"}
EVIDENCE_FILES = ["summary.json", "logs.json", "console.json", "network.json", "page.json", "screenshot.png"]
PLACEHOLDER_TARGETS = {"https://example.com", "http://example.com"}
SECRET_REDACTION = "[REDACTED]"
SECRET_PATTERNS = (
    re.compile(r"(?i)((?:x-hermes-token|authorization|api[_-]?key|access[_-]?token|token)\s*[:=]\s*['\"]?)([^'\"\s,;}]+)"),
    re.compile(r"(?i)(bearer\s+)([^'\"\s,;}]+)"),
)
LOGGER = logging.getLogger("riseos_agent_orchestrator")


class HermesWritebackClient(Protocol):
    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> Any:
        ...

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> Any:
        ...


class HermesDispatchHTTPClient(Protocol):
    async def post_job(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        ...


class HermesDispatchRegistry(Protocol):
    def claim_hermes_dispatch(self, dispatch_key: str) -> bool:
        ...

    def mark_hermes_dispatch(self, dispatch_key: str) -> None:
        ...


class HermesDispatchResult(BaseModel):
    attempted: bool = False
    success: bool = False
    status: Literal["PASSED", "FAILED", "BLOCKED", "SKIPPED"] = "SKIPPED"
    hermes_node: Literal["M2", "DGX"] = "M2"
    dispatch_key: str | None = None
    correlation_id: str | None = None
    skipped_reason: str | None = None
    error: str | None = None
    message: str | None = None
    comment: str | None = None
    label: str | None = None
    job_id: str | None = None


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
        response = await self._http_client.post(
            f"{base_url.rstrip('/')}/api/v1/jobs",
            headers={"X-Hermes-Token": token},
            json=payload,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {"raw": data}

    async def aclose(self) -> None:
        await self._http_client.aclose()


hermes_dispatch_registry = InMemoryHermesDispatchRegistry()


async def dispatch_hermes_runtime_validation(
    parsed: ParsedGitHubEvent,
    settings: Settings,
    *,
    slack_client: SlackIssueDispatchClient | None = None,
    github_client: HermesWritebackClient | None = None,
    hermes_client: HermesDispatchHTTPClient | None = None,
    registry: HermesDispatchRegistry = hermes_dispatch_registry,
) -> HermesDispatchResult:
    explicit = _explicit_hermes_command(parsed.comment_body)
    labels_request_hermes = _labels_request_hermes(parsed.labels, explicit=explicit)
    route = _route_reason(parsed)
    node = _hermes_node(parsed.labels)
    _log_hermes_decision(
        parsed,
        settings,
        "hermes_route_evaluated",
        node=node,
        route=route,
        explicit_command=explicit,
        labels_request_hermes=labels_request_hermes,
        runtime_label_match=bool(set(parsed.labels) & HERMES_RUNTIME_LABELS),
        lifecycle_label_match=bool(set(parsed.labels) & HERMES_LIFECYCLE_LABELS),
        terminal_label_match=bool(set(parsed.labels) & TERMINAL_LABELS),
        bb2_blocked=BB2_BLOCK_LABEL in set(parsed.labels),
    )
    if route is None:
        return HermesDispatchResult(hermes_node=node, skipped_reason="Event does not require Hermes runtime validation.")

    correlation_id = _hermes_correlation_id(parsed, node=node)
    dispatch_key = _dispatch_key(parsed, settings, node=node)
    disabled = _dispatch_disabled(settings, node=node)
    missing_config = _missing_config(settings, node=node)
    target_error = _target_url_error(settings.hermes_default_target)
    eligibility_blocker = _eligibility_blocker(
        dispatch_key=dispatch_key,
        disabled=disabled,
        missing_config=missing_config,
        target_error=target_error,
    )
    _log_hermes_decision(
        parsed,
        settings,
        "hermes_dispatch_eligibility_evaluated",
        node=node,
        route=route,
        dispatch_key=dispatch_key,
        dispatch_key_available=dispatch_key is not None,
        dispatch_enabled=eligibility_blocker is None,
        disabled_reason=disabled,
        missing_config=missing_config,
        target_error=target_error,
        eligibility_blocker=eligibility_blocker,
        hermes_target=settings.hermes_default_target,
    )
    if dispatch_key is None:
        return HermesDispatchResult(
            hermes_node=node,
            correlation_id=correlation_id,
            skipped_reason="Hermes dispatch key could not be determined.",
        )

    if disabled:
        return HermesDispatchResult(
            hermes_node=node,
            dispatch_key=dispatch_key,
            correlation_id=correlation_id,
            skipped_reason=disabled,
        )

    if node == "DGX":
        result = HermesDispatchResult(
            attempted=True,
            success=False,
            status="BLOCKED",
            hermes_node=node,
            dispatch_key=dispatch_key,
            correlation_id=correlation_id,
            error="Hermes DGX dispatch is not supported yet.",
            label="agent-blocked",
        )
        return await _notify_and_writeback(parsed, settings, result, slack_client=slack_client, github_client=github_client)

    if missing_config:
        result = HermesDispatchResult(
            attempted=True,
            success=False,
            status="BLOCKED",
            hermes_node=node,
            dispatch_key=dispatch_key,
            correlation_id=correlation_id,
            error=missing_config,
            label="agent-blocked",
        )
        return await _notify_and_writeback(parsed, settings, result, slack_client=slack_client, github_client=github_client)

    if target_error:
        result = HermesDispatchResult(
            attempted=True,
            success=False,
            status="BLOCKED",
            hermes_node=node,
            dispatch_key=dispatch_key,
            correlation_id=correlation_id,
            error=target_error,
            label="agent-blocked",
        )
        return await _notify_and_writeback(parsed, settings, result, slack_client=slack_client, github_client=github_client)

    if not registry.claim_hermes_dispatch(dispatch_key):
        return HermesDispatchResult(
            hermes_node=node,
            dispatch_key=dispatch_key,
            correlation_id=correlation_id,
            skipped_reason="Hermes validation was already dispatched for this item commit and target.",
        )

    trigger_label_error = await _apply_canonical_hermes_trigger_labels(
        parsed,
        settings,
        route=route,
        github_client=github_client,
    )

    owns_client = hermes_client is None
    hermes_client = hermes_client or HermesHTTPClient()
    payload = build_hermes_job_payload(parsed, settings, node=node, correlation_id=correlation_id, route=route)
    base_url = _node_base_url(settings, node)
    _log_hermes_decision(
        parsed,
        settings,
        "hermes_post_attempted",
        node=node,
        route=route,
        dispatch_key=dispatch_key,
        hermes_base_url=base_url,
        hermes_target=settings.hermes_default_target,
        payload_correlation_id=payload["correlationId"],
        payload_type=payload["type"],
    )
    try:
        response = await hermes_client.post_job(base_url, _node_token(settings, node), payload)
        result = _result_from_hermes_response(response, node=node, dispatch_key=dispatch_key, correlation_id=correlation_id)
        _log_hermes_decision(
            parsed,
            settings,
            "hermes_post_completed",
            node=node,
            route=route,
            dispatch_key=dispatch_key,
            status=result.status,
            success=result.success,
            job_id=result.job_id,
        )
        if trigger_label_error and not result.error:
            result.error = trigger_label_error
        if parsed.event_type == GitHubEventType.ISSUES and result.status == "FAILED":
            result.label = "agent-blocked"
        registry.mark_hermes_dispatch(dispatch_key)
    except Exception as exc:
        error = _redact_sensitive_text(str(exc), settings)
        _log_hermes_decision(
            parsed,
            settings,
            "hermes_post_failed",
            node=node,
            route=route,
            dispatch_key=dispatch_key,
            error=error,
        )
        result = HermesDispatchResult(
            attempted=True,
            success=False,
            status="BLOCKED",
            hermes_node=node,
            dispatch_key=dispatch_key,
            correlation_id=correlation_id,
            error=error,
            label="agent-blocked",
        )
    finally:
        if owns_client and hasattr(hermes_client, "aclose"):
            await hermes_client.aclose()

    return await _notify_and_writeback(parsed, settings, result, slack_client=slack_client, github_client=github_client)


def build_hermes_job_payload(
    parsed: ParsedGitHubEvent,
    settings: Settings,
    *,
    node: Literal["M2", "DGX"] = "M2",
    correlation_id: str | None = None,
    route: str | None = None,
) -> dict[str, Any]:
    commit_sha = parsed.head_sha or "unknown"
    subject_kind = _subject_kind(parsed)
    subject_number = _subject_number(parsed)
    branch = branch_from_parsed(parsed) or settings.work_branch
    labels = set(parsed.labels)
    if _is_circuit_pr(parsed):
        labels.update(CANONICAL_HERMES_TRIGGER_LABELS)

    payload: dict[str, Any] = {
        "source": "riseos-agent-orchestrator",
        "repo": parsed.repository,
        "subjectType": subject_kind,
        "commitSha": commit_sha,
        "branch": branch,
        "screenshotName": f"{subject_kind}-{subject_number}-validation.png",
        "labels": sorted(labels),
        "hermesNode": node,
        "trigger": route,
    }
    if subject_kind == "issue":
        payload["issueNumber"] = subject_number
    else:
        payload["prNumber"] = subject_number

    return {
        "type": "playwright",
        "dryRun": False,
        "targetUrl": settings.hermes_default_target,
        "correlationId": correlation_id or _hermes_correlation_id(parsed, node=node),
        "payload": payload,
    }


def build_hermes_slack_message(parsed: ParsedGitHubEvent, result: HermesDispatchResult, settings: Settings) -> str:
    repo = _sanitize_slack_text(parsed.repository or "unknown repo")
    subject_number = _subject_number(parsed) or "unknown"
    subject_label = _github_subject_label(parsed)
    labels = ", ".join(_sanitize_slack_text(label) for label in parsed.labels) if parsed.labels else "none"
    if result.status == "BLOCKED":
        reason = _sanitize_slack_text(_redact_sensitive_text(result.error or result.skipped_reason or "Hermes validation could not run.", settings))
        return (
            "Hermes validation blocked\n"
            f"Reason: {reason}\n"
            f"Repo: {repo}\n"
            f"{subject_label}: #{subject_number}\n"
            f"Node: {result.hermes_node}\n"
            f"Correlation ID: {_sanitize_slack_text(result.correlation_id or 'unknown')}"
        )
    if result.status in {"PASSED", "FAILED"}:
        return (
            "Hermes validation complete\n"
            f"Repo: {repo}\n"
            f"{subject_label}: #{subject_number}\n"
            f"Status: {result.status}\n"
            f"Job ID: {_sanitize_slack_text(result.job_id or 'unknown')}\n"
            f"Evidence: {', '.join(EVIDENCE_FILES)}"
        )
    return (
        "Hermes validation requested\n"
        f"Repo: {repo}\n"
        f"{subject_label}: #{subject_number}\n"
        f"Labels: {labels}\n"
        f"Node: {result.hermes_node}\n"
        f"Correlation ID: {_sanitize_slack_text(result.correlation_id or 'unknown')}"
    )


def build_hermes_pr_comment(parsed: ParsedGitHubEvent, result: HermesDispatchResult, settings: Settings) -> str:
    commit_sha = parsed.head_sha or "unknown"
    evidence = "\n".join(f"- {item}" for item in EVIDENCE_FILES)
    verified = "Hermes dispatch routing completed and produced this validation status."
    if result.status == "BLOCKED":
        verified = "Orchestrator detected that Hermes validation could not run."
    return (
        "## Hermes Runtime Validation\n\n"
        f"Status: {result.status}\n"
        f"Hermes node: {result.hermes_node}\n"
        f"Target: {_redact_sensitive_text(settings.hermes_default_target, settings)}\n"
        f"Job ID: {result.job_id or 'not-created'}\n"
        f"Correlation ID: {result.correlation_id or 'unknown'}\n"
        f"Commit: {commit_sha}\n\n"
        "### Evidence\n"
        f"{evidence}\n\n"
        "### VERIFIED\n"
        f"- {verified}\n"
        "- This label is runtime evidence only and is not merge approval.\n\n"
        "### ASSUMED\n"
        "- Runtime target is the configured Hermes default target.\n"
        "- Hermes artifacts are stored outside this repository or referenced by the returned job.\n\n"
        "### UNVERIFIED\n"
        f"- {_redact_sensitive_text(result.error, settings) or 'No additional unverified items reported by Hermes.'}\n"
    )


def _route_reason(parsed: ParsedGitHubEvent) -> str | None:
    explicit = _explicit_hermes_command(parsed.comment_body)
    if parsed.event_type == GitHubEventType.ISSUE_COMMENT:
        if parsed.action == "created" and parsed.pull_request_number and explicit:
            return "pr_comment_hermes_validate"
        return None
    if parsed.event_type == GitHubEventType.ISSUES:
        if parsed.action == "labeled" and _labels_request_hermes(parsed.labels, explicit=explicit):
            return "issue_labeled_hermes_validate"
        return None
    if parsed.event_type == GitHubEventType.PULL_REQUEST:
        if parsed.action not in {"labeled", "unlabeled", *CIRCUIT_HERMES_PR_ACTIONS}:
            return None
        if _labels_request_hermes(parsed.labels, explicit=explicit):
            return f"pull_request_{parsed.action}"
        if parsed.action in CIRCUIT_HERMES_PR_ACTIONS and _is_circuit_pr(parsed):
            return f"pull_request_{parsed.action}_circuit_hermes"
        return None
    if parsed.event_type == GitHubEventType.PULL_REQUEST_REVIEW:
        if parsed.action == "submitted" and _labels_request_hermes(parsed.labels, explicit=explicit):
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
    return (
        parsed.event_type == GitHubEventType.PULL_REQUEST
        and parsed.repository is not None
        and parsed.head_repo_full_name == parsed.repository
        and parsed.base_repo_full_name == parsed.repository
        and parsed.head_ref == CIRCUIT_WORK_BRANCH
        and parsed.base_ref == CIRCUIT_BASE_BRANCH
    )


def _missing_canonical_hermes_trigger_labels(labels: list[str]) -> list[str]:
    existing = set(labels)
    return [label for label in CANONICAL_HERMES_TRIGGER_LABELS if label not in existing]


async def _apply_canonical_hermes_trigger_labels(
    parsed: ParsedGitHubEvent,
    settings: Settings,
    *,
    route: str,
    github_client: HermesWritebackClient | None,
) -> str | None:
    if not settings.enable_github_writeback or github_client is None:
        return None
    if not route.endswith("_circuit_hermes"):
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
    if "dgx" in normalized and normalized & DGX_LABELS:
        return "DGX"
    return "M2"


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


def _dispatch_key(parsed: ParsedGitHubEvent, settings: Settings, *, node: Literal["M2", "DGX"]) -> str | None:
    repo = parsed.repository
    subject_number = _subject_number(parsed)
    commit_sha = parsed.head_sha or "unknown"
    if not repo or not subject_number:
        return None
    return f"{node}:{repo}:{_subject_kind(parsed)}:{subject_number}:sha:{commit_sha}:target:{settings.hermes_default_target}"


def _hermes_correlation_id(parsed: ParsedGitHubEvent, *, node: Literal["M2", "DGX"]) -> str:
    repo = (parsed.repository or "unknown/unknown").replace("/", "-")
    subject_number = _subject_number(parsed) or "unknown"
    short_sha = (parsed.head_sha or "unknown")[:7]
    return f"hermes-{node.lower()}-{repo}-{_subject_kind(parsed)}-{subject_number}-{short_sha}"


def _eligibility_blocker(
    *,
    dispatch_key: str | None,
    disabled: str | None,
    missing_config: str | None,
    target_error: str | None,
) -> str | None:
    if dispatch_key is None:
        return "dispatch_key_missing"
    return disabled or missing_config or target_error


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
    names = (
        "github_webhook_secret",
        "github_token",
        "github_app_private_key_path",
        "openai_api_key",
        "orchestrator_admin_token",
        "slack_webhook_url",
        "slack_bot_token",
        "hermes_token",
        "hermes_m2_token",
        "hermes_dgx_token",
    )
    values: list[str] = []
    for name in names:
        value = getattr(settings, name, None)
        if isinstance(value, str) and len(value) >= 4:
            values.append(value)
    return values


def _log_hermes_decision(
    parsed: ParsedGitHubEvent,
    settings: Settings,
    event: str,
    *,
    node: Literal["M2", "DGX"],
    route: str | None = None,
    **fields: Any,
) -> None:
    payload = {
        "event": event,
        "correlation_id": _hermes_correlation_id(parsed, node=node),
        "orchestrator_correlation_id": correlation_id_from_parsed(parsed),
        "github_event": str(parsed.event_type),
        "repo_full_name": parsed.repository,
        "action": parsed.action,
        "action_label": parsed.action_label,
        "commit_sha": parsed.head_sha,
        "issue_number": parsed.issue_number,
        "pr_number": parsed.pull_request_number,
        "labels": sorted(set(parsed.labels)),
        "hermes_node": node,
        "route": route,
        **fields,
    }
    sanitized = _redact_sensitive_value(payload, settings)
    LOGGER.info(json.dumps({key: value for key, value in sanitized.items() if value is not None or key == "route"}, sort_keys=True, default=str))


def _result_from_hermes_response(
    response: dict[str, Any],
    *,
    node: Literal["M2", "DGX"],
    dispatch_key: str,
    correlation_id: str,
) -> HermesDispatchResult:
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
    return HermesDispatchResult(
        attempted=True,
        success=success,
        status=status,
        hermes_node=node,
        dispatch_key=dispatch_key,
        correlation_id=correlation_id,
        label=label,
        job_id=str(job_id) if job_id else None,
    )


async def _notify_and_writeback(
    parsed: ParsedGitHubEvent,
    settings: Settings,
    result: HermesDispatchResult,
    *,
    slack_client: SlackIssueDispatchClient | None,
    github_client: HermesWritebackClient | None,
) -> HermesDispatchResult:
    request_message = build_hermes_slack_message(parsed, HermesDispatchResult(hermes_node=result.hermes_node, correlation_id=result.correlation_id), settings)
    final_message = build_hermes_slack_message(parsed, result, settings)
    result.message = final_message

    owns_slack = slack_client is None
    if settings.slack_webhook_url or settings.slack_bot_token:
        slack_client = slack_client or SlackClient(webhook_url=settings.slack_webhook_url, bot_token=settings.slack_bot_token)
        try:
            await slack_client.post_message(channel=settings.slack_channel, text=request_message)
            if final_message != request_message:
                await slack_client.post_message(channel=settings.slack_channel, text=final_message)
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
