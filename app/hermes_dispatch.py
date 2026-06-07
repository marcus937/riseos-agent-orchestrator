from __future__ import annotations

from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from app.config import Settings
from app.correlation import branch_from_parsed
from app.github_events import GitHubEventType, ParsedGitHubEvent
from app.slack_issue_dispatch import SlackClient, SlackIssueDispatchClient, _sanitize_slack_text

HERMES_RUNTIME_LABELS = {"runtime-agent", "playwright", "evidence", "testing"}
HERMES_LIFECYCLE_LABELS = {"bb-review-needed", "agent-review", "agent-ready", "agent-next"}
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
    route = _route_reason(parsed)
    if route is None:
        return HermesDispatchResult(skipped_reason="Event does not require Hermes runtime validation.")

    node = _hermes_node(parsed.labels)
    correlation_id = _hermes_correlation_id(parsed, node=node)
    dispatch_key = _dispatch_key(parsed, settings, node=node)
    if dispatch_key is None:
        return HermesDispatchResult(
            hermes_node=node,
            correlation_id=correlation_id,
            skipped_reason="PR dispatch key could not be determined.",
        )

    disabled = _dispatch_disabled(settings, node=node)
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

    missing_config = _missing_config(settings, node=node)
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

    target_error = _target_url_error(settings.hermes_default_target)
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
            skipped_reason="Hermes validation was already dispatched for this PR commit and target.",
        )

    owns_client = hermes_client is None
    hermes_client = hermes_client or HermesHTTPClient()
    payload = build_hermes_job_payload(parsed, settings, node=node, correlation_id=correlation_id, route=route)
    try:
        response = await hermes_client.post_job(_node_base_url(settings, node), _node_token(settings, node), payload)
        result = _result_from_hermes_response(response, node=node, dispatch_key=dispatch_key, correlation_id=correlation_id)
        registry.mark_hermes_dispatch(dispatch_key)
    except Exception as exc:
        result = HermesDispatchResult(
            attempted=True,
            success=False,
            status="BLOCKED",
            hermes_node=node,
            dispatch_key=dispatch_key,
            correlation_id=correlation_id,
            error=str(exc),
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
    pr_number = parsed.pull_request_number or parsed.issue_number
    branch = branch_from_parsed(parsed) or settings.work_branch
    labels = sorted(set(parsed.labels))
    return {
        "type": "playwright",
        "dryRun": False,
        "targetUrl": settings.hermes_default_target,
        "correlationId": correlation_id or _hermes_correlation_id(parsed, node=node),
        "payload": {
            "source": "riseos-agent-orchestrator",
            "repo": parsed.repository,
            "prNumber": pr_number,
            "commitSha": commit_sha,
            "branch": branch,
            "screenshotName": f"pr-{pr_number}-validation.png",
            "labels": labels,
            "hermesNode": node,
            "trigger": route,
        },
    }


def build_hermes_slack_message(parsed: ParsedGitHubEvent, result: HermesDispatchResult, settings: Settings) -> str:
    repo = _sanitize_slack_text(parsed.repository or "unknown repo")
    pr_number = parsed.pull_request_number or parsed.issue_number or "unknown"
    labels = ", ".join(_sanitize_slack_text(label) for label in parsed.labels) if parsed.labels else "none"
    if result.status == "BLOCKED":
        reason = _sanitize_slack_text(result.error or result.skipped_reason or "Hermes validation could not run.")
        return (
            "Hermes validation blocked\n"
            f"Reason: {reason}\n"
            f"Repo: {repo}\n"
            f"PR: #{pr_number}\n"
            f"Node: {result.hermes_node}\n"
            f"Correlation ID: {_sanitize_slack_text(result.correlation_id or 'unknown')}"
        )
    if result.status in {"PASSED", "FAILED"}:
        return (
            "Hermes validation complete\n"
            f"Repo: {repo}\n"
            f"PR: #{pr_number}\n"
            f"Status: {result.status}\n"
            f"Job ID: {_sanitize_slack_text(result.job_id or 'unknown')}\n"
            f"Evidence: {', '.join(EVIDENCE_FILES)}"
        )
    return (
        "Hermes validation requested\n"
        f"Repo: {repo}\n"
        f"PR: #{pr_number}\n"
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
        f"Target: {settings.hermes_default_target}\n"
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
        f"- {result.error or 'No additional unverified items reported by Hermes.'}\n"
    )


def _route_reason(parsed: ParsedGitHubEvent) -> str | None:
    explicit = _explicit_hermes_command(parsed.comment_body)
    if parsed.event_type == GitHubEventType.ISSUE_COMMENT:
        if parsed.action == "created" and parsed.pull_request_number and explicit:
            return "pr_comment_hermes_validate"
        return None
    if parsed.event_type == GitHubEventType.PULL_REQUEST:
        if parsed.action not in {"labeled", "unlabeled", "opened", "synchronize", "ready_for_review"}:
            return None
        if _labels_request_hermes(parsed.labels, explicit=explicit):
            return f"pull_request_{parsed.action}"
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


def _dispatch_key(parsed: ParsedGitHubEvent, settings: Settings, *, node: Literal["M2", "DGX"]) -> str | None:
    repo = parsed.repository
    pr_number = parsed.pull_request_number or parsed.issue_number
    commit_sha = parsed.head_sha or "unknown"
    if not repo or not pr_number:
        return None
    return f"{node}:{repo}:pr:{pr_number}:sha:{commit_sha}:target:{settings.hermes_default_target}"


def _hermes_correlation_id(parsed: ParsedGitHubEvent, *, node: Literal["M2", "DGX"]) -> str:
    repo = (parsed.repository or "unknown/unknown").replace("/", "-")
    pr_number = parsed.pull_request_number or parsed.issue_number or "unknown"
    short_sha = (parsed.head_sha or "unknown")[:7]
    return f"hermes-{node.lower()}-{repo}-pr-{pr_number}-{short_sha}"


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
            result.error = result.error or str(exc)
        finally:
            if owns_slack and slack_client is not None and hasattr(slack_client, "aclose"):
                await slack_client.aclose()

    if github_client is not None and settings.enable_github_writeback and result.label:
        pr_number = parsed.pull_request_number or parsed.issue_number
        if parsed.repository and pr_number:
            comment = build_hermes_pr_comment(parsed, result, settings)
            result.comment = comment
            try:
                await github_client.post_issue_comment(parsed.repository, pr_number, comment)
                await github_client.apply_label(parsed.repository, pr_number, result.label)
            except Exception as exc:
                result.error = result.error or str(exc)

    return result
