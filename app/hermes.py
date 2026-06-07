from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.config import Settings
from app.github_events import GitHubEventType, ParsedGitHubEvent
from app.review_queue import ReviewProcessResponse, ReviewWorkItem

HERMES_TRIGGER_LABELS = {"runtime-agent", "playwright", "evidence", "testing"}
HERMES_REVIEW_LABELS = {"bb-review-needed", "agent-review"}
HERMES_COMMENT_TRIGGERS = ("/hermes validate", "needs hermes validation", "run hermes validation")

LABEL_AGENT_VERIFIED = "agent-verified"
LABEL_AGENT_BLOCKED = "agent-blocked"
LABEL_AGENT_REVISIONS = "agent-revisions"

HERMES_ALLOWED_HOSTS = {"example.com", "localhost", "127.0.0.1"}
HERMES_ALLOWED_SUFFIXES = (".vercel.app", ".tailscale.ts.net")


class HermesDispatchClient(Protocol):
    async def status(self) -> dict[str, Any]:
        ...

    async def create_job(self, request: "HermesValidationRequest") -> dict[str, Any]:
        ...

    async def get_job(self, job_id: str) -> dict[str, Any]:
        ...

    async def get_evidence(self, job_id: str) -> dict[str, Any]:
        ...


class HermesWritebackClient(Protocol):
    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...


class HermesSlackClient(Protocol):
    async def post_message(self, *, channel: str, text: str) -> None:
        ...


class HermesValidationRequest(BaseModel):
    type: str = "playwright"
    dryRun: bool = False
    targetUrl: str
    correlationId: str
    payload: dict[str, Any] = Field(default_factory=dict)


class HermesDispatchResult(BaseModel):
    attempted: bool = False
    success: bool = False
    skipped_reason: str | None = None
    error: str | None = None
    job_id: str | None = None
    correlation_id: str | None = None
    status: str | None = None
    node: str | None = None
    target_url: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    comment_body: str | None = None
    label: str | None = None
    slack_message: str | None = None


@dataclass
class InMemoryHermesDispatchRegistry:
    _correlation_ids: set[str] = field(default_factory=set)

    def claim(self, correlation_id: str) -> bool:
        if correlation_id in self._correlation_ids:
            return False
        self._correlation_ids.add(correlation_id)
        return True

    def reset(self) -> None:
        self._correlation_ids.clear()


class HermesClient:
    def __init__(self, *, base_url: str, token: str, http_client: Any | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._http_client = http_client
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/status")

    async def create_job(self, request: HermesValidationRequest) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/jobs", json=request.model_dump(mode="json"))

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/jobs/{job_id}")

    async def get_evidence(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/evidence/{job_id}")

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._client.request(method, f"{self._base_url}{path}", headers={"X-Hermes-Token": self._token}, **kwargs)
        response.raise_for_status()
        if not response.content:
            return {}
        payload = response.json()
        return payload if isinstance(payload, dict) else {"data": payload}

    @property
    def _client(self) -> Any:
        if self._http_client is None:
            import httpx

            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client


hermes_dispatch_registry = InMemoryHermesDispatchRegistry()


def should_dispatch_hermes(parsed: ParsedGitHubEvent) -> bool:
    labels = set(parsed.labels)
    if parsed.event_type == GitHubEventType.PULL_REQUEST and parsed.action in {"opened", "synchronize", "labeled"}:
        return bool(labels & HERMES_TRIGGER_LABELS) and bool(labels & HERMES_REVIEW_LABELS)
    if parsed.event_type == GitHubEventType.ISSUE_COMMENT and parsed.pull_request_number:
        return _contains_hermes_comment_trigger(parsed.comment_body)
    return False


def build_hermes_correlation_id(item: ReviewWorkItem) -> str:
    repo = (item.repo_full_name or "unknown-repo").replace("/", "-")
    number = item.pr_number or item.issue_number or 0
    short_sha = (item.commit_sha or "unknown")[:7]
    return f"hermes-{repo}-pr-{number}-{short_sha}"


def build_hermes_validation_request(item: ReviewWorkItem, settings: Settings) -> HermesValidationRequest:
    correlation_id = build_hermes_correlation_id(item)
    target_url = settings.hermes_default_target
    return HermesValidationRequest(
        targetUrl=target_url,
        correlationId=correlation_id,
        payload={
            "screenshotName": f"pr-{item.pr_number or item.issue_number}-validation.png",
            "source": "orchestrator",
            "repo": item.repo_full_name,
            "prNumber": item.pr_number,
            "issueNumber": item.issue_number,
            "commitSha": item.commit_sha,
            "branch": item.branch,
            "validationType": "playwright",
        },
    )


async def dispatch_hermes_validation(
    response: ReviewProcessResponse,
    settings: Settings,
    *,
    hermes_client: HermesDispatchClient | None = None,
    github_client: HermesWritebackClient | None = None,
    slack_client: HermesSlackClient | None = None,
    registry: InMemoryHermesDispatchRegistry = hermes_dispatch_registry,
) -> HermesDispatchResult:
    item = response.work_item
    if not settings.hermes_enable_dispatch:
        return HermesDispatchResult(skipped_reason="Hermes dispatch is disabled.")
    if not item.repo_full_name or not item.pr_number or not item.commit_sha:
        return HermesDispatchResult(attempted=True, error="repo_full_name, pr_number, and commit_sha are required for Hermes dispatch.")
    if not settings.hermes_base_url or not settings.hermes_token:
        return HermesDispatchResult(attempted=True, error="HERMES_BASE_URL and HERMES_TOKEN are required for Hermes dispatch.")

    request = build_hermes_validation_request(item, settings)
    if not is_allowed_hermes_target(request.targetUrl):
        return HermesDispatchResult(attempted=True, correlation_id=request.correlationId, target_url=request.targetUrl, error="Hermes target URL is not allowlisted.")
    if not registry.claim(request.correlationId):
        return HermesDispatchResult(skipped_reason="Hermes dispatch already claimed for this PR commit.", correlation_id=request.correlationId)

    owns_client = hermes_client is None
    hermes_client = hermes_client or HermesClient(base_url=settings.hermes_base_url, token=settings.hermes_token)
    try:
        node = await hermes_client.status()
        job = await hermes_client.create_job(request)
        job_id = _job_id(job)
        job_status = await hermes_client.get_job(job_id) if job_id else job
        evidence = await hermes_client.get_evidence(job_id) if job_id else {}
        result = _result_from_hermes_payload(request, node, job_status, evidence, job_id)
    except Exception as exc:
        result = HermesDispatchResult(
            attempted=True,
            success=False,
            correlation_id=request.correlationId,
            target_url=request.targetUrl,
            error=str(exc),
            label=LABEL_AGENT_BLOCKED,
        )
    finally:
        if owns_client and hasattr(hermes_client, "aclose"):
            await hermes_client.aclose()  # type: ignore[attr-defined]

    result.comment_body = build_hermes_comment(result)
    result.slack_message = build_hermes_slack_message(item, result)
    if github_client:
        await github_client.post_issue_comment(item.repo_full_name, item.pr_number, result.comment_body)
        await github_client.apply_label(item.repo_full_name, item.pr_number, result.label or LABEL_AGENT_BLOCKED)
    if slack_client:
        await slack_client.post_message(channel=settings.slack_channel, text=result.slack_message)

    return result


def build_hermes_comment(result: HermesDispatchResult) -> str:
    evidence_files = _markdown_list(result.evidence.get("files") or result.evidence.get("evidenceFiles") or [])
    evidence_summary = result.evidence.get("summary") or result.evidence.get("runtimeHealthSummary") or "Not provided"
    screenshot = result.evidence.get("screenshotName") or result.evidence.get("screenshot") or "Not provided"
    verified = "- Hermes dispatch reached the configured validation node and collected an evidence response." if result.success else "- Hermes dispatch failed or validation did not pass."
    assumed = "- The configured Hermes target is an approved preview, local, simulator, or explicit test target."
    unverified = "- BB2 and human review remain required before merge."
    return (
        "## Hermes Runtime Validation\n\n"
        f"Status: {result.status or ('PASS' if result.success else 'BLOCKED')}\n"
        f"Job ID: {result.job_id or 'Not available'}\n"
        f"Hermes node: {result.node or 'Not available'}\n"
        f"Target URL: {result.target_url or 'Not available'}\n"
        f"Correlation ID: {result.correlation_id or 'Not available'}\n"
        f"Screenshot artifact: {screenshot}\n\n"
        "Evidence files:\n"
        f"{evidence_files}\n\n"
        "Evidence API response:\n"
        f"{evidence_summary}\n\n"
        "VERIFIED\n\n"
        f"{verified}\n\n"
        "ASSUMED\n\n"
        f"{assumed}\n\n"
        "UNVERIFIED\n\n"
        f"{unverified}"
    )


def build_hermes_slack_message(item: ReviewWorkItem, result: HermesDispatchResult) -> str:
    return (
        "Hermes validation complete\n"
        f"Repo: {item.repo_full_name or 'unknown'}\n"
        f"PR: {item.pr_number or 'unknown'}\n"
        f"Commit: {item.commit_sha or 'unknown'}\n"
        f"Status: {result.status or ('PASS' if result.success else 'BLOCKED')}\n"
        f"Job ID: {result.job_id or 'Not available'}\n"
        f"Evidence: {result.correlation_id or 'Not available'}"
    )


def is_allowed_hermes_target(target_url: str) -> bool:
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return host in HERMES_ALLOWED_HOSTS or any(host.endswith(suffix) for suffix in HERMES_ALLOWED_SUFFIXES)


def hermes_label_for_status(status: str | None) -> str:
    normalized = (status or "").lower()
    if normalized in {"pass", "passed", "success", "succeeded", "complete", "completed"}:
        return LABEL_AGENT_VERIFIED
    if normalized in {"needs_changes", "revisions", "changes_required"}:
        return LABEL_AGENT_REVISIONS
    return LABEL_AGENT_BLOCKED


def _contains_hermes_comment_trigger(body: str | None) -> bool:
    normalized = (body or "").lower()
    return any(trigger in normalized for trigger in HERMES_COMMENT_TRIGGERS)


def _result_from_hermes_payload(
    request: HermesValidationRequest,
    node: dict[str, Any],
    job_status: dict[str, Any],
    evidence: dict[str, Any],
    job_id: str | None,
) -> HermesDispatchResult:
    status = str(job_status.get("status") or evidence.get("status") or "BLOCKED")
    label = hermes_label_for_status(status)
    return HermesDispatchResult(
        attempted=True,
        success=label == LABEL_AGENT_VERIFIED,
        job_id=job_id,
        correlation_id=request.correlationId,
        status=status,
        node=str(node.get("node") or node.get("name") or node.get("hostname") or "Hermes"),
        target_url=request.targetUrl,
        evidence=evidence,
        label=label,
    )


def _job_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("jobId") or payload.get("id")
    return str(value) if value else None


def _markdown_list(items: Any) -> str:
    if not isinstance(items, list) or not items:
        return "- None"
    return "\n".join(f"- {item}" for item in items)
