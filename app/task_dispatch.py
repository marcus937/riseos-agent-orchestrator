from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel

from app.reviewer.decision import ReviewDecisionType


LABEL_AGENT_TASK = "agent-task"
LABEL_AGENT_READY = "agent-ready"
LABEL_AGENT_WORKING = "agent-working"
LABEL_BB2_REVIEW_NEEDED = "bb2-review-needed"
LABEL_BB2_APPROVED = "bb2-approved"
LABEL_BB2_NEEDS_CHANGES = "bb2-needs-changes"
LABEL_BB2_BLOCKED = "bb2-blocked"
LABEL_AGENT_NEXT = "agent-next"

AGENT_TASK_LABELS = {
    LABEL_AGENT_TASK,
    LABEL_AGENT_READY,
    LABEL_AGENT_WORKING,
    LABEL_BB2_REVIEW_NEEDED,
    LABEL_BB2_APPROVED,
    LABEL_BB2_NEEDS_CHANGES,
    LABEL_BB2_BLOCKED,
    LABEL_AGENT_NEXT,
}

BB2_DECISION_LABELS = {
    ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW: LABEL_BB2_APPROVED,
    ReviewDecisionType.NEEDS_CHANGES: LABEL_BB2_NEEDS_CHANGES,
    ReviewDecisionType.BLOCKED: LABEL_BB2_BLOCKED,
    ReviewDecisionType.ESCALATE_TO_MARCUS: LABEL_BB2_BLOCKED,
}


class TaskDispatchClient(Protocol):
    async def list_open_issues(
        self,
        repo_full_name: str,
        *,
        labels: list[str] | None = None,
        sort: str = "created",
        direction: str = "asc",
    ) -> list[dict[str, Any]]:
        ...

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...


class AgentTaskIssue(BaseModel):
    number: int
    title: str
    body: str | None = None
    labels: list[str]
    created_at: datetime | None = None


class TaskDispatchResult(BaseModel):
    attempted: bool = False
    success: bool = False
    issue_number: int | None = None
    error: str | None = None
    assignment_body: str | None = None


def should_dispatch_next_task(decision: ReviewDecisionType) -> bool:
    return decision == ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW


async def list_agent_ready_issues(repo_full_name: str, client: TaskDispatchClient) -> list[AgentTaskIssue]:
    issues = await client.list_open_issues(
        repo_full_name,
        labels=[LABEL_AGENT_TASK, LABEL_AGENT_READY],
        sort="created",
        direction="asc",
    )
    ready: list[AgentTaskIssue] = []
    for raw_issue in issues:
        if raw_issue.get("pull_request") is not None:
            continue
        labels = _label_names(raw_issue.get("labels"))
        if LABEL_AGENT_TASK not in labels or LABEL_AGENT_READY not in labels:
            continue
        if _has_existing_owner(labels):
            continue
        ready.append(
            AgentTaskIssue(
                number=int(raw_issue["number"]),
                title=str(raw_issue.get("title") or f"Issue {raw_issue['number']}"),
                body=raw_issue.get("body") if isinstance(raw_issue.get("body"), str) else None,
                labels=sorted(labels),
                created_at=_parse_datetime(raw_issue.get("created_at")),
            )
        )
    return sorted(ready, key=lambda issue: (issue.created_at or datetime.min, issue.number))


async def select_next_agent_task(repo_full_name: str, client: TaskDispatchClient) -> AgentTaskIssue | None:
    issues = await list_agent_ready_issues(repo_full_name, client)
    return issues[0] if issues else None


async def post_circuit_assignment(
    repo_full_name: str,
    issue_number: int,
    assignment_body: str,
    client: TaskDispatchClient,
) -> None:
    await client.apply_label(repo_full_name, issue_number, LABEL_AGENT_NEXT)
    await client.post_issue_comment(repo_full_name, issue_number, assignment_body)


async def dispatch_next_agent_task(
    repo_full_name: str | None,
    client: TaskDispatchClient,
    *,
    enabled: bool,
) -> TaskDispatchResult:
    if not enabled:
        return TaskDispatchResult()
    if not repo_full_name:
        return TaskDispatchResult(attempted=True, error="repo_full_name is required for task dispatch.")

    try:
        issue = await select_next_agent_task(repo_full_name, client)
        if issue is None:
            return TaskDispatchResult(
                attempted=True,
                success=False,
                error="No queued unclaimed agent-ready issue found",
            )
        assignment_body = build_circuit_assignment_body(issue)
        await post_circuit_assignment(repo_full_name, issue.number, assignment_body, client)
    except Exception as exc:
        return TaskDispatchResult(attempted=True, success=False, error=str(exc))

    return TaskDispatchResult(
        attempted=True,
        success=True,
        issue_number=issue.number,
        assignment_body=assignment_body,
    )


def build_circuit_assignment_body(issue: AgentTaskIssue) -> str:
    task_summary = _trim_issue_body(issue.body)
    return (
        "## Circuit Assignment\n\n"
        f"Issue: #{issue.number} - {issue.title}\n\n"
        "Target integration branch: `agent-integration`\n\n"
        "Working branch: create a dedicated `circuit/<task>` branch for this issue.\n\n"
        "Reminders:\n"
        "- Work only on the dedicated `circuit/<task>` branch.\n"
        "- Open a PR into `agent-integration` when the task is ready for review.\n"
        "- Request BB2 review on the PR.\n"
        "- Never commit directly to `main`.\n"
        "- Never merge or deploy.\n"
        "- Comment `Status: Done` with the PR URL and completed commit SHA when finished.\n\n"
        "Task summary:\n"
        f"{task_summary}"
    )


def _has_existing_owner(labels: set[str]) -> bool:
    return bool({LABEL_AGENT_NEXT, LABEL_AGENT_WORKING, LABEL_BB2_BLOCKED} & labels)


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


def _parse_datetime(raw_value: Any) -> datetime | None:
    if not isinstance(raw_value, str) or not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _trim_issue_body(body: str | None) -> str:
    if not body or not body.strip():
        return "No issue body provided. Use the issue title as the task summary."
    text = body.strip()
    if len(text) <= 4000:
        return text
    return f"{text[:4000].rstrip()}\n\n[Task summary truncated for assignment comment.]"
