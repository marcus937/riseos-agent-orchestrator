from __future__ import annotations

from typing import Any, Literal

from app.config import Settings
from app.correlation import branch_from_parsed
from app.github_events import GitHubEventType, ParsedGitHubEvent

MISSING_PR_PREVIEW_REASON = "No successful Vercel preview deployment is available for this PR head SHA yet."
PENDING_TARGET_SOURCE = "vercel_preview_pending"


def _subject_number(parsed: ParsedGitHubEvent, impl: Any) -> int | None:
    value = impl._subject_number(parsed)
    return value if isinstance(value, int) else None


def _requires_pr_preview(parsed: ParsedGitHubEvent, impl: Any) -> bool:
    if parsed.event_type in {GitHubEventType.PULL_REQUEST, GitHubEventType.PULL_REQUEST_REVIEW}:
        return True
    if parsed.event_type == GitHubEventType.ISSUE_COMMENT and _subject_number(parsed, impl) is not None:
        return True
    return False


def _can_check_preview_metadata(github_client: Any | None) -> bool:
    return github_client is not None and (
        hasattr(github_client, "list_commit_statuses") or hasattr(github_client, "list_check_runs_for_ref")
    )


def _branch_name(parsed: ParsedGitHubEvent, settings: Settings) -> str | None:
    return branch_from_parsed(parsed) or getattr(settings, "work_branch", None)


def _commit_sha(parsed: ParsedGitHubEvent) -> str | None:
    return parsed.head_sha or None


def install_preview_guard(impl: Any, namespace: dict[str, Any]) -> None:
    original_resolver = impl._resolve_hermes_target_url
    original_dispatch = impl.dispatch_hermes_runtime_validation
    original_payload_builder = impl.build_hermes_job_payload

    async def _resolve_hermes_target_url(
        parsed: ParsedGitHubEvent,
        settings: Settings,
        *,
        github_client: Any | None,
    ) -> tuple[str, str]:
        node = impl._hermes_node(parsed.labels)
        payload_preview_url = impl._preview_url_from_payload(parsed.raw)
        if payload_preview_url:
            impl._log_hermes_decision(
                parsed,
                settings,
                "hermes_target_resolved",
                node=node,
                target_url=payload_preview_url,
                target_url_source="webhook_payload_preview_url",
                hermes_dispatched=True,
                pr_number=_subject_number(parsed, impl),
                branch=_branch_name(parsed, settings),
                commit_sha=_commit_sha(parsed),
            )
            return payload_preview_url, "webhook_payload_preview_url"

        github_preview_url = await impl._preview_url_from_github_commit_metadata(parsed, github_client)
        if github_preview_url:
            impl._log_hermes_decision(
                parsed,
                settings,
                "hermes_target_resolved",
                node=node,
                target_url=github_preview_url,
                target_url_source="github_commit_preview_url",
                hermes_dispatched=True,
                pr_number=_subject_number(parsed, impl),
                branch=_branch_name(parsed, settings),
                commit_sha=_commit_sha(parsed),
            )
            return github_preview_url, "github_commit_preview_url"

        if _requires_pr_preview(parsed, impl) and _can_check_preview_metadata(github_client):
            impl._log_hermes_decision(
                parsed,
                settings,
                "hermes_preview_pending",
                node=node,
                target_url=None,
                target_url_source=PENDING_TARGET_SOURCE,
                hermes_dispatched=False,
                fallback_reason=MISSING_PR_PREVIEW_REASON,
                pr_number=_subject_number(parsed, impl),
                branch=_branch_name(parsed, settings),
                commit_sha=_commit_sha(parsed),
            )
            return "", PENDING_TARGET_SOURCE

        target_url, target_source = await original_resolver(parsed, settings, github_client=github_client)
        impl._log_hermes_decision(
            parsed,
            settings,
            "hermes_target_resolved",
            node=node,
            target_url=target_url,
            target_url_source=target_source,
            hermes_dispatched=True,
            pr_number=_subject_number(parsed, impl),
            branch=_branch_name(parsed, settings),
            commit_sha=_commit_sha(parsed),
        )
        return target_url, target_source

    def build_hermes_job_payload(
        parsed: ParsedGitHubEvent,
        settings: Settings,
        *,
        node: Literal["M2", "DGX"] = "M2",
        correlation_id: str | None = None,
        route: str | None = None,
        target_url: str | None = None,
        target_source: str | None = None,
    ) -> dict[str, Any]:
        if target_url is None and _requires_pr_preview(parsed, impl) and target_source == PENDING_TARGET_SOURCE:
            raise ValueError(MISSING_PR_PREVIEW_REASON)
        return original_payload_builder(
            parsed,
            settings,
            node=node,
            correlation_id=correlation_id,
            route=route,
            target_url=target_url,
            target_source=target_source,
        )

    async def dispatch_hermes_runtime_validation(
        parsed: ParsedGitHubEvent,
        settings: Settings,
        *,
        slack_client: Any | None = None,
        github_client: Any | None = None,
        hermes_client: Any | None = None,
        registry: Any = impl.hermes_dispatch_registry,
    ) -> Any:
        route = impl._route_reason(parsed)
        node = impl._hermes_node(parsed.labels)
        if route is None:
            return await original_dispatch(
                parsed,
                settings,
                slack_client=slack_client,
                github_client=github_client,
                hermes_client=hermes_client,
                registry=registry,
            )

        target_url, target_source = await _resolve_hermes_target_url(parsed, settings, github_client=github_client)
        if target_source == PENDING_TARGET_SOURCE:
            correlation_id = impl._hermes_correlation_id(parsed, node=node)
            impl._log_hermes_decision(
                parsed,
                settings,
                "hermes_dispatch_eligibility_evaluated",
                node=node,
                route=route,
                dispatch_enabled=False,
                eligibility_blocker=MISSING_PR_PREVIEW_REASON,
                target_url=None,
                target_url_source=target_source,
                hermes_dispatched=False,
                fallback_reason=MISSING_PR_PREVIEW_REASON,
                pr_number=_subject_number(parsed, impl),
                branch=_branch_name(parsed, settings),
                commit_sha=_commit_sha(parsed),
            )
            return impl.HermesDispatchResult(
                hermes_node=node,
                correlation_id=correlation_id,
                target_url=None,
                target_source=target_source,
                preview_url=None,
                skipped_reason=MISSING_PR_PREVIEW_REASON,
            )

        return await original_dispatch(
            parsed,
            settings,
            slack_client=slack_client,
            github_client=github_client,
            hermes_client=hermes_client,
            registry=registry,
        )

    impl._resolve_hermes_target_url = _resolve_hermes_target_url
    impl.build_hermes_job_payload = build_hermes_job_payload
    impl.dispatch_hermes_runtime_validation = dispatch_hermes_runtime_validation
    namespace["_resolve_hermes_target_url"] = _resolve_hermes_target_url
    namespace["build_hermes_job_payload"] = build_hermes_job_payload
    namespace["dispatch_hermes_runtime_validation"] = dispatch_hermes_runtime_validation
