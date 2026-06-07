from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.pr_workflow_state import bb2_decision_transition_labels
from app.review_queue import ReviewProcessResponse
from app.task_dispatch import BB2_DECISION_LABELS


class GitHubWritebackClient(Protocol):
    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...


class GitHubWritebackResult(BaseModel):
    attempted: bool = False
    success: bool = False
    error: str | None = None
    comment_body: str | None = None
    label: str | None = None
    labels: list[str] = Field(default_factory=list)


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

    labels = bb2_decision_transition_labels(response.decision.decision, item.labels)
    label = labels[0] if labels else DECISION_LABELS[response.decision.decision]
    comment_body = build_writeback_comment(response, labels=labels)
    try:
        await client.post_issue_comment(item.repo_full_name, target_number, comment_body)
        for next_label in labels:
            await client.apply_label(item.repo_full_name, target_number, next_label)
    except Exception as exc:
        return GitHubWritebackResult(
            attempted=True,
            success=False,
            error=str(exc),
            comment_body=comment_body,
            label=label,
            labels=labels,
        )

    return GitHubWritebackResult(
        attempted=True,
        success=True,
        comment_body=comment_body,
        label=label,
        labels=labels,
    )


def build_writeback_comment(response: ReviewProcessResponse, *, labels: list[str] | None = None) -> str:
    decision = response.decision
    required_changes = "\n".join(f"- {item}" for item in decision.required_changes) or "- None"
    changed_files = "\n".join(f"- {path}" for path in response.changed_files) or "- None"
    diff_summary = response.diff_summary or "Not available"
    label_lines = "\n".join(f"- {label}" for label in labels or []) or "- None"
    return (
        "## Review Decision\n"
        f"{decision.decision.value}\n\n"
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
