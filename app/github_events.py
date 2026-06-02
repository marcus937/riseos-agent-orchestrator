from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class GitHubEventType(StrEnum):
    ISSUE_COMMENT = "issue_comment"
    PUSH = "push"
    PULL_REQUEST = "pull_request"


class ParsedGitHubEvent(BaseModel):
    event_type: GitHubEventType
    action: str | None = None
    repository: str | None = None
    sender: str | None = None
    issue_number: int | None = None
    pull_request_number: int | None = None
    ref: str | None = None
    before: str | None = None
    after: str | None = None
    head_sha: str | None = None
    head_ref: str | None = None
    base_ref: str | None = None
    comment_body: str | None = None
    labels: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class UnsupportedGitHubEventError(ValueError):
    pass


def _repo_name(payload: dict[str, Any]) -> str | None:
    repo = payload.get("repository") or {}
    full_name = repo.get("full_name")
    return str(full_name) if full_name else None


def _sender_login(payload: dict[str, Any]) -> str | None:
    sender = payload.get("sender") or {}
    login = sender.get("login")
    return str(login) if login else None


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

    if event_type == GitHubEventType.ISSUE_COMMENT:
        issue = payload.get("issue") or {}
        labels = issue.get("labels") or []
        return ParsedGitHubEvent(
            **base,
            issue_number=issue.get("number"),
            pull_request_number=issue.get("number") if issue.get("pull_request") else None,
            labels=[str(label.get("name")) for label in labels if isinstance(label, dict) and label.get("name")],
            comment_body=(payload.get("comment") or {}).get("body"),
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
    labels = pull_request.get("labels") or []
    return ParsedGitHubEvent(
        **base,
        pull_request_number=pull_request.get("number") or payload.get("number"),
        head_sha=(pull_request.get("head") or {}).get("sha"),
        head_ref=(pull_request.get("head") or {}).get("ref"),
        base_ref=(pull_request.get("base") or {}).get("ref"),
        labels=[str(label.get("name")) for label in labels if isinstance(label, dict) and label.get("name")],
    )


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
