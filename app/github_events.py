import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class GitHubEventType(StrEnum):
    ISSUE_COMMENT = "issue_comment"
    ISSUES = "issues"
    PING = "ping"
    PUSH = "push"
    PULL_REQUEST = "pull_request"
    PULL_REQUEST_REVIEW = "pull_request_review"


class ParsedGitHubEvent(BaseModel):
    event_type: GitHubEventType
    action: str | None = None
    repository: str | None = None
    sender: str | None = None
    issue_number: int | None = None
    issue_title: str | None = None
    issue_url: str | None = None
    issue_state: str | None = None
    action_label: str | None = None
    pull_request_number: int | None = None
    pull_request_merged: bool | None = None
    ref: str | None = None
    before: str | None = None
    after: str | None = None
    head_sha: str | None = None
    head_ref: str | None = None
    head_repo_full_name: str | None = None
    base_ref: str | None = None
    base_repo_full_name: str | None = None
    comment_body: str | None = None
    labels: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class UnsupportedGitHubEventError(ValueError):
    pass


COMMENT_COMMIT_SHA_PATTERN = re.compile(
    r"(?im)^\s*(?:commit(?:[\s_-]+)?sha|sha)\s*:\s*([0-9a-f]{7,40})"
)


def _repo_name(payload: dict[str, Any]) -> str | None:
    repo = payload.get("repository") or {}
    full_name = repo.get("full_name")
    return str(full_name) if full_name else None


def _sender_login(payload: dict[str, Any]) -> str | None:
    sender = payload.get("sender") or {}
    login = sender.get("login")
    return str(login) if login else None


def _label_names(raw_labels: Any) -> list[str]:
    labels = raw_labels or []
    return [str(label.get("name")) for label in labels if isinstance(label, dict) and label.get("name")]


def _action_label(payload: dict[str, Any]) -> str | None:
    label = payload.get("label") or {}
    name = label.get("name")
    return str(name) if name else None


def _full_name(raw_repo: Any) -> str | None:
    if not isinstance(raw_repo, dict):
        return None
    full_name = raw_repo.get("full_name")
    return str(full_name) if full_name else None


def parse_github_event(event_name: str, payload: dict[str, Any]) -> ParsedGitHubEvent:
    try:
        event_type = GitHubEventType(event_name)
    except ValueError as exc:
        raise UnsupportedGitHubEventError(f"Unsupported GitHub event: {event_name}") from exc

    base = {
        "event_type": event_type,
        "action": payload.get("action"),
        "repository": _repo_name(payload),
        "sender": _sender_login(payload),
        "raw": payload,
    }

    if event_type == GitHubEventType.PING:
        return ParsedGitHubEvent(**base)

    if event_type == GitHubEventType.ISSUE_COMMENT:
        issue = payload.get("issue") or {}
        comment_body = (payload.get("comment") or {}).get("body")
        return ParsedGitHubEvent(
            **base,
            issue_number=issue.get("number"),
            issue_title=issue.get("title"),
            issue_url=issue.get("html_url"),
            issue_state=issue.get("state"),
            pull_request_number=issue.get("number") if issue.get("pull_request") else None,
            labels=_label_names(issue.get("labels")),
            comment_body=comment_body,
            head_sha=extract_commit_sha_from_comment(comment_body),
        )

    if event_type == GitHubEventType.ISSUES:
        issue = payload.get("issue") or {}
        return ParsedGitHubEvent(
            **base,
            issue_number=issue.get("number"),
            issue_title=issue.get("title"),
            issue_url=issue.get("html_url"),
            issue_state=issue.get("state"),
            action_label=_action_label(payload),
            labels=_label_names(issue.get("labels")),
        )

    if event_type == GitHubEventType.PUSH:
        return ParsedGitHubEvent(
            **base,
            ref=payload.get("ref"),
            before=payload.get("before"),
            after=payload.get("after"),
            head_sha=payload.get("after"),
        )

    pull_request = payload.get("pull_request") or {}
    head = pull_request.get("head") or {}
    base_ref = pull_request.get("base") or {}
    return ParsedGitHubEvent(
        **base,
        pull_request_number=pull_request.get("number") or payload.get("number"),
        pull_request_merged=pull_request.get("merged"),
        action_label=_action_label(payload),
        head_sha=head.get("sha"),
        head_ref=head.get("ref"),
        head_repo_full_name=_full_name(head.get("repo")),
        base_ref=base_ref.get("ref"),
        base_repo_full_name=_full_name(base_ref.get("repo")),
        labels=_label_names(pull_request.get("labels")),
    )


def extract_commit_sha_from_comment(body: str | None) -> str | None:
    if not body:
        return None
    match = COMMENT_COMMIT_SHA_PATTERN.search(body)
    return match.group(1) if match else None


class WebhookAcceptedResponse(BaseModel):
    status: Literal["accepted"] = "accepted"
    event_accepted: bool = True
    event_type: GitHubEventType
    repository: str | None = None
    repo: str | None = None
    action: str | None = None
    task_state: str | None = None
    issue_number: int | None = None
    pull_request_number: int | None = None
    commit_sha: str | None = None
    review_context: dict[str, Any] | None = None
    next_intended_action: str | None = None
