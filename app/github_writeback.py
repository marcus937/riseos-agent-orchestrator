from __future__ import annotations

import logging
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.pr_workflow_state import (
    LABEL_BB2_APPROVED,
    LABEL_BB2_BLOCKED,
    LABEL_BB2_NEEDS_CHANGES,
    LABEL_BB_REVIEW_NEEDED,
    LABEL_READY_TO_MERGE,
    bb2_decision_transition_labels,
)
from app.review_queue import ReviewProcessResponse
from app.reviewer.decision import ReviewDecisionType
from app.task_dispatch import BB2_DECISION_LABELS, LABEL_BB2_REVIEW_NEEDED

logger = logging.getLogger(__name__)

BB2_STATUS_COMMENT_MARKER = "<!-- jarvis-bb2-review-status -->"
BB2_PENDING_LABEL = "bb2-pending"
BB2_STATE_LABELS = {
    BB2_PENDING_LABEL,
    LABEL_BB2_APPROVED,
    LABEL_BB2_NEEDS_CHANGES,
    LABEL_BB2_BLOCKED,
}
BB2_REVIEW_REQUEST_LABELS = {LABEL_BB_REVIEW_NEEDED, LABEL_BB2_REVIEW_NEEDED}
BB2_TRANSIENT_LABELS = BB2_STATE_LABELS | BB2_REVIEW_REQUEST_LABELS | {LABEL_READY_TO_MERGE}


class GitHubWritebackClient(Protocol):
    async def fetch_issue(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        ...

    async def list_issue_comments(self, repo_full_name: str, issue_number: int) -> list[dict[str, Any]]:
        ...

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...

    async def update_issue_comment(self, repo_full_name: str, comment_id: int, body: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...

    async def remove_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...


class GitHubWritebackResult(BaseModel):
    attempted: bool = False
    success: bool = False
    error: str | None = None
    comment_body: str | None = None
    label: str | None = None
    labels: list[str] = Field(default_factory=list)
    removed_labels: list[str] = Field(default_factory=list)
    comment_updated: bool = False
    updated_comment_id: int | None = None


DECISION_LABELS = BB2_DECISION_LABELS


async def writeback_review_decision(
    response: ReviewProcessResponse,
    client: GitHubWritebackClient,
) -> GitHubWritebackResult:
    item = response.work_item
    if not item.repo_full_name:
        return GitHubWritebackResult(error="repo_full_name is required for GitHub writeback.")

    target_number = item.pr_number or item.issue_number
    if target_number is None:
        return GitHubWritebackResult(error="issue_number or pr_number is required for GitHub writeback.")

    current_labels = await _current_issue_labels(client, item.repo_full_name, target_number, fallback=item.labels)
    labels = _target_labels(response.decision.decision, current_labels)
    label = labels[0]
    labels_to_remove = _labels_to_remove(current_labels, labels)
    comment_body = build_writeback_comment(response, labels=labels)
    try:
        for next_label in labels:
            await client.apply_label(item.repo_full_name, target_number, next_label)
    except Exception as exc:
        logger.error(
            "BB2 review writeback failed before cleanup repo=%s issue=%s outcome=%s labels=%s error=%s",
            item.repo_full_name,
            target_number,
            response.decision.decision.value,
            labels,
            exc,
        )
        return GitHubWritebackResult(
            attempted=True,
            success=False,
            error=str(exc),
            comment_body=comment_body,
            label=label,
            labels=labels,
        )

    comment_updated = False
    updated_comment_id: int | None = None
    try:
        comment_updated, updated_comment_id = await _upsert_status_comment(
            client,
            item.repo_full_name,
            target_number,
            comment_body,
        )
    except Exception as exc:
        logger.warning(
            "BB2 status comment writeback failed after labels were applied repo=%s issue=%s outcome=%s error=%s",
            item.repo_full_name,
            target_number,
            response.decision.decision.value,
            exc,
        )

    removed_labels: list[str] = []
    for old_label in labels_to_remove:
        try:
            await client.remove_label(item.repo_full_name, target_number, old_label)
            removed_labels.append(old_label)
        except Exception as exc:
            logger.warning(
                "BB2 stale label cleanup failed after current state was applied repo=%s issue=%s label=%s error=%s",
                item.repo_full_name,
                target_number,
                old_label,
                exc,
            )

    logger.info(
        "BB2 review writeback updated labels repo=%s issue=%s outcome=%s removed=%s applied=%s comment_updated=%s",
        item.repo_full_name,
        target_number,
        response.decision.decision.value,
        removed_labels,
        labels,
        comment_updated,
    )
    return GitHubWritebackResult(
        attempted=True,
        success=True,
        comment_body=comment_body,
        label=label,
        labels=labels,
        removed_labels=removed_labels,
        comment_updated=comment_updated,
        updated_comment_id=updated_comment_id,
    )


def bb2_state_label_for_decision(decision: ReviewDecisionType | str) -> str:
    normalized = str(decision.value if isinstance(decision, ReviewDecisionType) else decision).lower()
    if normalized in {"approved_for_human_review", "approved"}:
        return LABEL_BB2_APPROVED
    if normalized in {"needs_changes", "rejected", "changes_requested"}:
        return LABEL_BB2_NEEDS_CHANGES
    if normalized in {"pending", "queued", "review_in_progress"}:
        return BB2_PENDING_LABEL
    return LABEL_BB2_BLOCKED


def build_writeback_comment(response: ReviewProcessResponse, *, labels: list[str] | None = None) -> str:
    decision = response.decision
    required_changes = "\n".join(f"- {item}" for item in decision.required_changes) or "- None"
    changed_files = "\n".join(f"- {path}" for path in response.changed_files) or "- None"
    diff_summary = response.diff_summary or "Not available"
    label_lines = "\n".join(f"- {label}" for label in labels or []) or "- None"
    review_source = response.reviewer_model or "dry-run-review-processor"
    return (
        f"{BB2_STATUS_COMMENT_MARKER}\n"
        "## Review Decision\n"
        f"{decision.decision.value}\n\n"
        "## Review Source\n"
        f"{review_source}\n\n"
        "## Risk Level\n"
        f"{decision.risk_level.value}\n\n"
        "## Summary\n"
        f"{decision.summary}\n\n"
        "## Required Changes\n"
        f"{required_changes}\n\n"
        "## Changed Files\n"
        f"{changed_files}\n\n"
        "## Diff Summary\n"
        f"{diff_summary}\n\n"
        "## Workflow Labels\n"
        f"{label_lines}\n\n"
        "## Dry-run Status\n"
        f"{response.work_item.status.value}\n\n"
        "## Human Review Required\n"
        f"{decision.human_review_required}"
    )


def _target_labels(decision: ReviewDecisionType, current_labels: set[str]) -> list[str]:
    state_label = bb2_state_label_for_decision(decision)
    stable_labels = (current_labels - BB2_TRANSIENT_LABELS) | {state_label}
    transition_labels = bb2_decision_transition_labels(decision, stable_labels)
    labels = [state_label]
    for transition_label in transition_labels:
        if transition_label not in labels and transition_label not in BB2_REVIEW_REQUEST_LABELS:
            labels.append(transition_label)
    return labels


def _labels_to_remove(current_labels: set[str], target_labels: list[str]) -> list[str]:
    target = set(target_labels)
    return sorted(label for label in current_labels if label in BB2_TRANSIENT_LABELS and label not in target)


async def _current_issue_labels(
    client: GitHubWritebackClient,
    repo_full_name: str,
    issue_number: int,
    *,
    fallback: list[str],
) -> set[str]:
    try:
        issue = await client.fetch_issue(repo_full_name, issue_number)
    except Exception as exc:
        logger.warning(
            "Could not fetch current GitHub labels before BB2 writeback; using work item labels repo=%s issue=%s error=%s",
            repo_full_name,
            issue_number,
            exc,
        )
        return {label for label in fallback if label}
    return _label_names(issue.get("labels"))


async def _upsert_status_comment(
    client: GitHubWritebackClient,
    repo_full_name: str,
    issue_number: int,
    body: str,
) -> tuple[bool, int | None]:
    try:
        comments = await client.list_issue_comments(repo_full_name, issue_number)
    except Exception as exc:
        logger.warning(
            "Could not list GitHub comments before BB2 writeback; posting a new status comment repo=%s issue=%s error=%s",
            repo_full_name,
            issue_number,
            exc,
        )
        await client.post_issue_comment(repo_full_name, issue_number, body)
        return False, None

    existing = _find_status_comment(comments)
    if existing is None:
        await client.post_issue_comment(repo_full_name, issue_number, body)
        return False, None

    comment_id = int(existing["id"])
    await client.update_issue_comment(repo_full_name, comment_id, body)
    return True, comment_id


def _find_status_comment(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    for comment in reversed(comments):
        body = comment.get("body")
        if isinstance(body, str) and BB2_STATUS_COMMENT_MARKER in body and comment.get("id") is not None:
            return comment
    return None


def _label_names(raw_labels: Any) -> set[str]:
    names: set[str] = set()
    if not isinstance(raw_labels, list):
        return names
    for label in raw_labels:
        if isinstance(label, str):
            names.add(label)
        elif isinstance(label, dict) and isinstance(label.get("name"), str):
            names.add(label["name"])
    return names
