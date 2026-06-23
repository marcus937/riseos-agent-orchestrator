from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.circuit_agent_trigger import CircuitAgentTriggerClient, wake_circuit_agent_for_work
from app.config import Settings, get_settings
from app.reviewer.decision import ReviewDecisionType
from app.task_dependencies import dependency_state_for_issue


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

DEFAULT_AGENT_BUS_PRIORITY = "normal"


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

    async def fetch_issue(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        ...

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...


class AgentBusDispatchClient(Protocol):
    async def create_work_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


class AgentTaskIssue(BaseModel):
    number: int
    title: str
    body: str | None = None
    labels: list[str]
    created_at: datetime | None = None
    url: str | None = None
    dependency_count: int = 0
    dependencies_satisfied: bool = True
    blocked_by: list[int] = Field(default_factory=list)


class TaskDispatchResult(BaseModel):
    attempted: bool = False
    success: bool = False
    issue_number: int | None = None
    error: str | None = None
    assignment_body: str | None = None
    dependency_count: int = 0
    dependencies_satisfied: bool = True
    blocked_by: list[int] = Field(default_factory=list)
    agent_bus_attempted: bool = False
    agent_bus_success: bool = False
    agent_bus_work_item_id: str | None = None
    agent_bus_error: str | None = None
    agent_bus_payload: dict[str, Any] | None = None
    lifecycle_events: list[str] = Field(default_factory=list)
    circuit_wakeup_attempted: bool = False
    circuit_wakeup_success: bool = False
    circuit_wakeup_error: str | None = None


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
        issue_number = int(raw_issue["number"])
        body = raw_issue.get("body") if isinstance(raw_issue.get("body"), str) else None
        dependency_state = await dependency_state_for_issue(repo_full_name, issue_number, body, client)
        if not dependency_state.dependencies_satisfied:
            continue
        ready.append(
            AgentTaskIssue(
                number=issue_number,
                title=str(raw_issue.get("title") or f"Issue {raw_issue['number']}"),
                body=body,
                labels=sorted(labels),
                created_at=_parse_datetime(raw_issue.get("created_at")),
                url=raw_issue.get("html_url") if isinstance(raw_issue.get("html_url"), str) else None,
                dependency_count=dependency_state.dependency_count,
                dependencies_satisfied=dependency_state.dependencies_satisfied,
                blocked_by=dependency_state.blocked_by,
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
    agent_bus_client: AgentBusDispatchClient | None = None,
    agent_bus_enabled: bool = False,
    owner_agent: str = "codex-m2",
    review_agent: str = "bb2",
    work_branch: str = "agent-integration",
    settings: Settings | None = None,
    target_agent: str = "circuit",
    circuit_trigger_client: CircuitAgentTriggerClient | None = None,

) -> TaskDispatchResult:
    if not enabled:
        return TaskDispatchResult()
    if not repo_full_name:
        return TaskDispatchResult(attempted=True, error="repo_full_name is required for task dispatch.")

    lifecycle_events: list[str] = []
    try:
        issue = await select_next_agent_task(repo_full_name, client)
        if issue is None:
            return TaskDispatchResult(
                attempted=True,
                success=False,
                error="No queued unclaimed agent-ready issue found",
            )

        agent_bus_attempted = False
        agent_bus_success = False
        agent_bus_work_item_id: str | None = None
        agent_bus_error: str | None = None
        agent_bus_payload = build_agent_bus_work_item_payload(
            repo_full_name,
            issue,
            owner_agent=owner_agent,
            review_agent=review_agent,
            work_branch=work_branch,
        )
        if agent_bus_enabled:
            lifecycle_events.append("agent_bus_dispatch_started")
            agent_bus_attempted = True
            if agent_bus_client is None:
                agent_bus_error = "Agent Bus dispatch is enabled but no Agent Bus client is configured."
            else:
                try:
                    agent_bus_response = await agent_bus_client.create_work_item(agent_bus_payload)
                    raw_work_item_id = agent_bus_response.get("work_item_id") or agent_bus_response.get("id")
                    agent_bus_work_item_id = str(raw_work_item_id) if raw_work_item_id else None
                    agent_bus_success = agent_bus_work_item_id is not None
                    if agent_bus_success:
                        lifecycle_events.append("agent_bus_dispatch_completed")
                    else:
                        agent_bus_error = "Agent Bus work item response did not include work_item_id."
                except Exception as exc:
                    agent_bus_error = str(exc)

        assignment_body = build_circuit_assignment_body(issue)
        await post_circuit_assignment(repo_full_name, issue.number, assignment_body, client)
        wakeup = await wake_circuit_agent_for_work(
            settings or get_settings(),
            target_agent=target_agent,
            repo_full_name=repo_full_name,
            issue_number=issue.number,
            client=circuit_trigger_client,
        )
    except Exception as exc:
        return TaskDispatchResult(attempted=True, success=False, error=str(exc), lifecycle_events=lifecycle_events)

    return TaskDispatchResult(
        attempted=True,
        success=True,
        issue_number=issue.number,
        assignment_body=assignment_body,
        dependency_count=issue.dependency_count,
        dependencies_satisfied=issue.dependencies_satisfied,
        blocked_by=issue.blocked_by,
        agent_bus_attempted=agent_bus_attempted,
        agent_bus_success=agent_bus_success,
        agent_bus_work_item_id=agent_bus_work_item_id,
        agent_bus_error=agent_bus_error,
        agent_bus_payload=agent_bus_payload,
        lifecycle_events=lifecycle_events,
        circuit_wakeup_attempted=wakeup.attempted,
        circuit_wakeup_success=wakeup.success,
        circuit_wakeup_error=wakeup.error,
    )


def build_agent_bus_work_item_payload(
    repo_full_name: str,
    issue: AgentTaskIssue,
    *,
    owner_agent: str,
    review_agent: str,
    work_branch: str,
) -> dict[str, Any]:
    priority = _priority_from_labels(issue.labels)
    return {
        "title": issue.title,
        "repository": repo_full_name,
        "issue_number": issue.number,
        "priority": priority,
        "owner_agent": owner_agent,
        "review_agent": review_agent,
        "metadata": {
            "objective": _trim_issue_body(issue.body),
            "branch": work_branch,
            "issue_url": issue.url,
            "source": "riseos-agent-orchestrator",
            "dispatch_label": LABEL_AGENT_NEXT,
            "labels": issue.labels,
            "dependency_count": issue.dependency_count,
            "dependencies_satisfied": issue.dependencies_satisfied,
            "blocked_by": issue.blocked_by,
            "routing": {
                "owner_agent": owner_agent,
                "owner_capabilities": ["coding", "github", "testing"],
                "owner_agent_type": "implementation",
                "review_agent": review_agent,
                "reviewer_capabilities": ["pr_review"],
                "reviewer_agent_type": "review",
            },
        },
    }


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


def _priority_from_labels(labels: list[str]) -> str:
    normalized = {label.lower() for label in labels}
    if {"urgent", "priority:urgent", "priority-urgent", "p0"} & normalized:
        return "urgent"
    if {"high", "priority:high", "priority-high", "p1"} & normalized:
        return "high"
    if {"low", "priority:low", "priority-low", "p3"} & normalized:
        return "low"
    return DEFAULT_AGENT_BUS_PRIORITY


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
