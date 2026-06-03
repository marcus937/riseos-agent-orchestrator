from typing import Any, Protocol

from pydantic import BaseModel

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

    label = DECISION_LABELS[response.decision.decision]
    comment_body = build_writeback_comment(response)
    try:
        await client.post_issue_comment(item.repo_full_name, target_number, comment_body)
        await client.apply_label(item.repo_full_name, target_number, label)
    except Exception as exc:
        return GitHubWritebackResult(
            attempted=True,
            success=False,
            error=str(exc),
            comment_body=comment_body,
            label=label,
        )

    return GitHubWritebackResult(
        attempted=True,
        success=True,
        comment_body=comment_body,
        label=label,
    )


def build_writeback_comment(response: ReviewProcessResponse) -> str:
    decision = response.decision
    required_changes = "\n".join(f"- {item}" for item in decision.required_changes) or "- None"
    changed_files = "\n".join(f"- {path}" for path in response.changed_files) or "- None"
    diff_summary = response.diff_summary or "Not available"
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
        "## Dry-run Status\n"
        f"{response.work_item.status.value}\n\n"
        "## Human Review Required\n"
        f"{decision.human_review_required}"
    )
