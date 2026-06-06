from __future__ import annotations

from hashlib import sha256
from typing import Any

from app.github_events import ParsedGitHubEvent


def branch_from_parsed(parsed: ParsedGitHubEvent) -> str | None:
    if parsed.head_ref:
        return parsed.head_ref
    if parsed.ref and parsed.ref.startswith("refs/heads/"):
        return parsed.ref.removeprefix("refs/heads/")
    return parsed.ref


def correlation_key_from_parsed(parsed: ParsedGitHubEvent) -> str:
    repo = parsed.repository or "unknown-repo"
    if parsed.pull_request_number:
        return f"{repo}:pr:{parsed.pull_request_number}"
    if parsed.issue_number:
        return f"{repo}:issue:{parsed.issue_number}"
    if parsed.head_sha:
        return f"{repo}:commit:{parsed.head_sha}"
    return f"{repo}:event:{parsed.event_type}"


def correlation_id_from_key(correlation_key: str | None) -> str | None:
    if not correlation_key:
        return None
    digest = sha256(correlation_key.encode("utf-8")).hexdigest()[:16]
    return f"orch-{digest}"


def correlation_id_from_parsed(parsed: ParsedGitHubEvent) -> str:
    return correlation_id_from_key(correlation_key_from_parsed(parsed)) or "orch-unknown"


def correlation_id_from_item(item: Any) -> str | None:
    repo = getattr(item, "repo_full_name", None) or "unknown-repo"
    pr_number = getattr(item, "pr_number", None)
    issue_number = getattr(item, "issue_number", None)
    commit_sha = getattr(item, "commit_sha", None)
    event_type = getattr(item, "event_type", None)
    if pr_number:
        key = f"{repo}:pr:{pr_number}"
    elif issue_number:
        key = f"{repo}:issue:{issue_number}"
    elif commit_sha:
        key = f"{repo}:commit:{commit_sha}"
    else:
        key = f"{repo}:event:{event_type}"
    return correlation_id_from_key(key)
