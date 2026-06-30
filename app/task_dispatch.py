from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.circuit_agent_trigger import CircuitAgentTriggerClient, wake_circuit_agent_for_work
from app.config import Settings, get_settings
from app.operational_logging import log_event
from app.reviewer.decision import ReviewDecisionType
from app.task_dependencies import dependency_state_for_issue
from app.workflow_continuation import (
    WorkflowContinuation,
    WorkflowContinuationStatus,
    WorkflowContinuationStore,
    log_continuation_event,
    workflow_continuation_idempotency_key,
)


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
WF_CHAIN_STEPS = tuple(f"WF{step}" for step in range(21, 30))
WF_CHAIN_OWNER_AGENT = "codex-m2"
WF_CHAIN_REVIEW_AGENT = "bb2"
WF_CHAIN_BASE_BRANCH = "agent-integration"
_EXISTING_WORK_STATUSES = {
    WorkflowContinuationStatus.DISPATCHED,
    WorkflowContinuationStatus.RUNNING,
    WorkflowContinuationStatus.WAITING_FOR_REVIEW,
    WorkflowContinuationStatus.COMPLETED,
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
    continuation_id: str | None = None
    continuation_status: str | None = None
    idempotency_key: str | None = None


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


async def dispatch_workflow_chain_continuation(
    item: Any,
    decision: ReviewDecisionType,
    *,
    enabled: bool,
    agent_bus_client: AgentBusDispatchClient | None,
    agent_bus_enabled: bool,
    continuation_store: WorkflowContinuationStore | None = None,
    base_branch: str = WF_CHAIN_BASE_BRANCH,
) -> TaskDispatchResult | None:
    missing_reason = workflow_chain_missing_metadata_reason(item)
    if missing_reason is not None:
        return _record_missing_metadata_continuation(item, continuation_store, base_branch=base_branch, reason=missing_reason)

    continuation_context = workflow_chain_continuation_for_decision(item, decision, base_branch=base_branch)
    if continuation_context is None:
        if workflow_chain_context_from_item(item, base_branch=base_branch) is not None:
            log_event("CONTINUATION_SKIPPED_FINAL_STEP", **_workflow_chain_log_context(item))
        return None
    if not enabled:
        return TaskDispatchResult()
    if continuation_store is None:
        return TaskDispatchResult(
            attempted=True,
            success=False,
            error="Workflow continuation store is required for workflow chain continuation.",
        )

    continuation, created = continuation_store.resolve_or_create_workflow_continuation(
        build_workflow_continuation_payload(continuation_context)
    )
    existing_work_item_id = continuation.next_work_item_id or continuation.current_work_item_id
    if decision == ReviewDecisionType.NEEDS_CHANGES and existing_work_item_id:
        continuation = continuation_store.mark_workflow_continuation_changes_requested(continuation.continuation_id)
        return _continuation_result(continuation, success=True, agent_bus_attempted=False)
    if not created and continuation.status in _EXISTING_WORK_STATUSES and existing_work_item_id:
        return _continuation_result(continuation, success=True, agent_bus_attempted=False)

    return await _dispatch_existing_workflow_continuation(
        continuation,
        agent_bus_client=agent_bus_client,
        agent_bus_enabled=agent_bus_enabled,
        continuation_store=continuation_store,
    )


async def resume_workflow_continuations(
    continuation_store: WorkflowContinuationStore,
    *,
    agent_bus_client: AgentBusDispatchClient | None,
    agent_bus_enabled: bool,
) -> list[TaskDispatchResult]:
    results: list[TaskDispatchResult] = []
    for continuation in continuation_store.list_retryable_workflow_continuations():
        result = await _dispatch_existing_workflow_continuation(
            continuation,
            agent_bus_client=agent_bus_client,
            agent_bus_enabled=agent_bus_enabled,
            continuation_store=continuation_store,
            include_dispatching=True,
            retry=True,
        )
        results.append(result)
    return results


async def _dispatch_existing_workflow_continuation(
    continuation: WorkflowContinuation,
    *,
    agent_bus_client: AgentBusDispatchClient | None,
    agent_bus_enabled: bool,
    continuation_store: WorkflowContinuationStore,
    include_dispatching: bool = False,
    retry: bool = False,
) -> TaskDispatchResult:
    payload = build_workflow_chain_work_item_payload(workflow_chain_context_from_continuation(continuation))
    if not agent_bus_enabled:
        continuation = continuation_store.mark_workflow_continuation_retry_pending(
            continuation.continuation_id,
            error="Agent Bus dispatch is required for workflow chain continuation.",
        )
        return _continuation_result(continuation, success=False, agent_bus_attempted=False, payload=payload)
    if agent_bus_client is None:
        continuation = continuation_store.mark_workflow_continuation_retry_pending(
            continuation.continuation_id,
            error="Agent Bus dispatch is enabled but no Agent Bus client is configured.",
        )
        return _continuation_result(continuation, success=False, agent_bus_attempted=True, payload=payload)

    locked, acquired = continuation_store.acquire_workflow_continuation_lock(
        continuation.continuation_id,
        include_dispatching=include_dispatching,
    )
    if not acquired:
        return _continuation_result(locked, success=bool(locked.next_work_item_id or locked.current_work_item_id), agent_bus_attempted=False, payload=payload)

    try:
        response = await agent_bus_client.create_work_item(payload)
        raw_work_item_id = response.get("work_item_id") or response.get("id")
        work_item_id = str(raw_work_item_id) if raw_work_item_id else ""
        if not work_item_id:
            raise RuntimeError("Agent Bus work item response did not include work_item_id.")
        dispatched = continuation_store.mark_workflow_continuation_dispatched(locked.continuation_id, work_item_id=work_item_id)
        if retry:
            log_continuation_event("CONTINUATION_RETRY_SUCCEEDED", dispatched)
        return _continuation_result(dispatched, success=True, agent_bus_attempted=True, payload=payload)
    except Exception as exc:
        failed = continuation_store.mark_workflow_continuation_retry_pending(locked.continuation_id, error=str(exc))
        return _continuation_result(failed, success=False, agent_bus_attempted=True, payload=payload)


def build_workflow_continuation_payload(context: dict[str, Any]) -> dict[str, Any]:
    idempotency_key = workflow_continuation_idempotency_key(
        workflow_chain_id=context["workflow_chain_id"],
        repository=context["repository"],
        pr_number=context["pr_number"],
        branch=context["branch"],
        next_workflow_step=context["next_workflow_step"],
    )
    return {
        "workflow_chain_id": context["workflow_chain_id"],
        "current_workflow_step": context["previous_workflow_step"],
        "next_workflow_step": context["next_workflow_step"],
        "workflow_steps": context.get("workflow_steps"),
        "repository": context["repository"],
        "pr_number": context["pr_number"],
        "branch": context["branch"],
        "base_branch": context.get("base_branch"),
        "previous_work_item_id": context.get("previous_work_item_id"),
        "current_work_item_id": context.get("current_work_item_id"),
        "next_work_item_id": context.get("next_work_item_id"),
        "idempotency_key": idempotency_key,
    }


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


def build_workflow_chain_work_item_payload(continuation: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "source": "riseos-agent-orchestrator",
        "dispatch_reason": continuation["dispatch_reason"],
        "workflow_chain_id": continuation.get("workflow_chain_id"),
        "workflow_step": continuation["next_workflow_step"],
        "previous_workflow_step": continuation["previous_workflow_step"],
        "workflow_steps": continuation.get("workflow_steps"),
        "workflow_sequence": continuation.get("workflow_steps"),
        "next_workflow_step": continuation.get("following_workflow_step"),
        "previous_work_item_id": continuation["previous_work_item_id"],
        "previous_review_queue_item_id": continuation.get("previous_review_queue_item_id"),
        "repository": continuation["repository"],
        "pr_number": continuation["pr_number"],
        "branch": continuation["branch"],
        "base_branch": continuation["base_branch"],
        "commit_sha": continuation.get("commit_sha"),
        "issue_number": continuation.get("issue_number"),
        "continuation_id": continuation.get("continuation_id"),
        "idempotency_key": continuation.get("idempotency_key"),
        "reuse_existing_pr": True,
        "create_new_pr": False,
        "open_new_pr": False,
        "merge_required_before_next_step": False,
        "routing": {
            "owner_agent": WF_CHAIN_OWNER_AGENT,
            "owner_capabilities": ["coding", "github", "testing"],
            "owner_agent_type": "implementation",
            "review_agent": WF_CHAIN_REVIEW_AGENT,
            "reviewer_capabilities": ["pr_review"],
            "reviewer_agent_type": "review",
        },
    }
    return _compact_payload(
        {
            "title": _workflow_chain_title(continuation),
            "repository": continuation["repository"],
            "issue_number": continuation.get("issue_number"),
            "pr_number": continuation["pr_number"],
            "priority": DEFAULT_AGENT_BUS_PRIORITY,
            "owner_agent": WF_CHAIN_OWNER_AGENT,
            "review_agent": WF_CHAIN_REVIEW_AGENT,
            "metadata": _compact_payload(metadata),
        }
    )


def workflow_chain_continuation_for_decision(
    item: Any,
    decision: ReviewDecisionType,
    *,
    base_branch: str = WF_CHAIN_BASE_BRANCH,
) -> dict[str, Any] | None:
    context = workflow_chain_context_from_item(item, base_branch=base_branch)
    if context is None:
        return None
    current_step = context["workflow_step"]
    if decision == ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW:
        next_step = next_workflow_chain_step(
            current_step,
            workflow_steps=context.get("workflow_steps"),
            explicit_next=context.get("next_workflow_step"),
        )
        if next_step is None:
            return None
        context["next_workflow_step"] = next_step
        context["following_workflow_step"] = next_workflow_chain_step(next_step, workflow_steps=context.get("workflow_steps"))
        context["dispatch_reason"] = "workflow_chain_approval_continuation"
        return context
    if decision == ReviewDecisionType.NEEDS_CHANGES:
        context["next_workflow_step"] = current_step
        context["following_workflow_step"] = next_workflow_chain_step(current_step, workflow_steps=context.get("workflow_steps"))
        context["dispatch_reason"] = "workflow_chain_needs_changes"
        return context
    return None


def workflow_chain_context_from_item(item: Any, *, base_branch: str = WF_CHAIN_BASE_BRANCH) -> dict[str, Any] | None:
    runtime_context = _dict_value(getattr(item, "runtime_validation_context", None))
    review_dispatch = _dict_value(runtime_context.get("review_dispatch"))
    workflow_step = _normalize_workflow_step(
        _first_present(
            review_dispatch.get("workflow_step"),
            review_dispatch.get("workflowStep"),
            runtime_context.get("workflow_step"),
            runtime_context.get("workflowStep"),
        )
    )
    workflow_chain_id = _string_or_none(
        _first_present(
            review_dispatch.get("workflow_chain_id"),
            review_dispatch.get("workflowChainId"),
            runtime_context.get("workflow_chain_id"),
        )
    )
    workflow_steps = _workflow_steps_from_context(review_dispatch, runtime_context)
    explicit_next = _normalize_workflow_step(
        _first_present(
            review_dispatch.get("next_workflow_step"),
            review_dispatch.get("nextWorkflowStep"),
            runtime_context.get("next_workflow_step"),
            runtime_context.get("nextWorkflowStep"),
        )
    )
    if not workflow_step or not workflow_chain_id:
        return None
    if not workflow_steps and explicit_next is None and workflow_step not in WF_CHAIN_STEPS:
        return None

    repo = _string_or_none(getattr(item, "repo_full_name", None)) or _string_or_none(
        _first_present(review_dispatch.get("repository"), runtime_context.get("repo"), runtime_context.get("repository"))
    )
    pr_number = _int_or_none(
        _first_present(getattr(item, "pr_number", None), review_dispatch.get("pr_number"), runtime_context.get("pr_number"))
    )
    branch = _string_or_none(
        _first_present(getattr(item, "branch", None), review_dispatch.get("branch"), runtime_context.get("branch"))
    )
    if not repo or pr_number is None or not branch:
        return None

    return {
        "repository": repo,
        "issue_number": _int_or_none(
            _first_present(getattr(item, "issue_number", None), review_dispatch.get("issue_number"), runtime_context.get("issue_number"))
        ),
        "pr_number": pr_number,
        "branch": branch,
        "base_branch": _string_or_none(
            _first_present(getattr(item, "base_branch", None), review_dispatch.get("base_branch"), runtime_context.get("base_branch"))
        )
        or base_branch,
        "commit_sha": _string_or_none(
            _first_present(getattr(item, "commit_sha", None), review_dispatch.get("commit_sha"), runtime_context.get("commit_sha"))
        ),
        "workflow_chain_id": workflow_chain_id,
        "workflow_step": workflow_step,
        "workflow_steps": workflow_steps,
        "next_workflow_step": explicit_next,
        "previous_workflow_step": workflow_step,
        "previous_work_item_id": _string_or_none(
            _first_present(
                getattr(item, "agent_bus_work_item_id", None),
                runtime_context.get("agent_bus_work_item_id"),
                runtime_context.get("work_item_id"),
                getattr(item, "id", None),
            )
        ),
        "previous_review_queue_item_id": _string_or_none(getattr(item, "id", None)),
    }


def workflow_chain_context_from_continuation(continuation: WorkflowContinuation) -> dict[str, Any]:
    workflow_steps = continuation.workflow_steps
    dispatch_reason = "workflow_chain_approval_continuation"
    if continuation.next_workflow_step == continuation.current_workflow_step:
        dispatch_reason = "workflow_chain_needs_changes"
    elif continuation.status == WorkflowContinuationStatus.RETRY_PENDING:
        dispatch_reason = "workflow_chain_retry"
    return {
        "repository": continuation.repository,
        "issue_number": None,
        "pr_number": continuation.pr_number,
        "branch": continuation.branch,
        "base_branch": continuation.base_branch or WF_CHAIN_BASE_BRANCH,
        "commit_sha": None,
        "workflow_chain_id": continuation.workflow_chain_id,
        "workflow_step": continuation.current_workflow_step,
        "workflow_steps": workflow_steps,
        "previous_workflow_step": continuation.current_workflow_step,
        "next_workflow_step": continuation.next_workflow_step,
        "following_workflow_step": next_workflow_chain_step(continuation.next_workflow_step, workflow_steps=workflow_steps),
        "previous_work_item_id": continuation.previous_work_item_id,
        "previous_review_queue_item_id": None,
        "continuation_id": continuation.continuation_id,
        "idempotency_key": continuation.idempotency_key,
        "dispatch_reason": dispatch_reason,
    }


def workflow_chain_missing_metadata_reason(item: Any) -> str | None:
    runtime_context = _dict_value(getattr(item, "runtime_validation_context", None))
    review_dispatch = _dict_value(runtime_context.get("review_dispatch"))
    if not runtime_context or not review_dispatch:
        return None
    fields = {
        "workflow_step": _first_present(
            review_dispatch.get("workflow_step"),
            review_dispatch.get("workflowStep"),
            runtime_context.get("workflow_step"),
            runtime_context.get("workflowStep"),
        ),
        "workflow_chain_id": _first_present(
            review_dispatch.get("workflow_chain_id"),
            review_dispatch.get("workflowChainId"),
            runtime_context.get("workflow_chain_id"),
        ),
        "repository": _first_present(getattr(item, "repo_full_name", None), review_dispatch.get("repository"), runtime_context.get("repository"), runtime_context.get("repo")),
        "pr_number": _first_present(getattr(item, "pr_number", None), review_dispatch.get("pr_number"), runtime_context.get("pr_number")),
        "branch": _first_present(getattr(item, "branch", None), review_dispatch.get("branch"), runtime_context.get("branch")),
    }
    chain_like = any(fields.values()) or runtime_context.get("source") == "runtime_validation_bb2_packet"
    if not chain_like:
        return None
    missing = [name for name, value in fields.items() if value in (None, "")]
    if missing:
        return "MISSING_WORKFLOW_METADATA"
    workflow_steps = _workflow_steps_from_context(review_dispatch, runtime_context)
    explicit_next = _first_present(review_dispatch.get("next_workflow_step"), review_dispatch.get("nextWorkflowStep"), runtime_context.get("next_workflow_step"), runtime_context.get("nextWorkflowStep"))
    workflow_step = _normalize_workflow_step(fields["workflow_step"])
    if not workflow_steps and not explicit_next and workflow_step not in WF_CHAIN_STEPS:
        return "MISSING_WORKFLOW_METADATA"
    return None


def workflow_chain_ready_to_merge_deferred(item: Any, decision: ReviewDecisionType) -> bool:
    if decision != ReviewDecisionType.APPROVED_FOR_HUMAN_REVIEW:
        return False
    context = workflow_chain_context_from_item(item)
    if context is None:
        return False
    return next_workflow_chain_step(
        context["workflow_step"],
        workflow_steps=context.get("workflow_steps"),
        explicit_next=context.get("next_workflow_step"),
    ) is not None


def workflow_chain_step_from_item(item: Any) -> str | None:
    context = workflow_chain_context_from_item(item)
    return context["workflow_step"] if context else None


def next_workflow_chain_step(
    workflow_step: str,
    *,
    workflow_steps: list[str] | None = None,
    explicit_next: str | None = None,
) -> str | None:
    if explicit_next:
        return _normalize_workflow_step(explicit_next)
    if workflow_steps:
        normalized = [_normalize_workflow_step(step) for step in workflow_steps]
        normalized = [step for step in normalized if step]
        if workflow_step in normalized:
            index = normalized.index(workflow_step)
            if index < len(normalized) - 1:
                return normalized[index + 1]
            return None
    if workflow_step in WF_CHAIN_STEPS:
        index = WF_CHAIN_STEPS.index(workflow_step)
        if index < len(WF_CHAIN_STEPS) - 1:
            return WF_CHAIN_STEPS[index + 1]
    return None


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


def _record_missing_metadata_continuation(
    item: Any,
    continuation_store: WorkflowContinuationStore | None,
    *,
    base_branch: str,
    reason: str,
) -> TaskDispatchResult:
    repo = _string_or_none(getattr(item, "repo_full_name", None)) or "UNKNOWN_REPOSITORY"
    branch = _string_or_none(getattr(item, "branch", None)) or "UNKNOWN_BRANCH"
    pr_number = _int_or_none(getattr(item, "pr_number", None)) or 0
    workflow_chain_id = f"UNKNOWN:{_string_or_none(getattr(item, 'id', None)) or 'untracked'}"
    idempotency_key = workflow_continuation_idempotency_key(
        workflow_chain_id=workflow_chain_id,
        repository=repo,
        pr_number=pr_number,
        branch=branch,
        next_workflow_step="UNKNOWN",
    )
    payload = {
        "workflow_chain_id": workflow_chain_id,
        "current_workflow_step": "UNKNOWN",
        "next_workflow_step": "UNKNOWN",
        "repository": repo,
        "pr_number": pr_number,
        "branch": branch,
        "base_branch": base_branch,
        "previous_work_item_id": _string_or_none(getattr(item, "agent_bus_work_item_id", None)) or _string_or_none(getattr(item, "id", None)),
        "idempotency_key": idempotency_key,
    }
    if continuation_store is not None:
        continuation = continuation_store.create_failed_workflow_continuation(payload, reason=reason)
        continuation_id = continuation.continuation_id
        continuation_status = continuation.status.value
    else:
        continuation_id = None
        continuation_status = WorkflowContinuationStatus.FAILED.value
    log_event(
        "CONTINUATION_METADATA_MISSING",
        workflow_chain_id=workflow_chain_id,
        current_workflow_step="UNKNOWN",
        next_workflow_step="UNKNOWN",
        continuation_id=continuation_id,
        idempotency_key=idempotency_key,
        work_item_id=payload["previous_work_item_id"],
        pr_number=pr_number,
        branch=branch,
        status=continuation_status,
        reason=reason,
    )
    return TaskDispatchResult(
        attempted=True,
        success=False,
        error=reason,
        continuation_id=continuation_id,
        continuation_status=continuation_status,
        idempotency_key=idempotency_key,
    )


def _continuation_result(
    continuation: WorkflowContinuation,
    *,
    success: bool,
    agent_bus_attempted: bool,
    payload: dict[str, Any] | None = None,
) -> TaskDispatchResult:
    work_item_id = continuation.next_work_item_id or continuation.current_work_item_id
    return TaskDispatchResult(
        attempted=True,
        success=success,
        issue_number=None,
        error=continuation.last_error,
        agent_bus_attempted=agent_bus_attempted,
        agent_bus_success=success and agent_bus_attempted,
        agent_bus_work_item_id=work_item_id,
        agent_bus_error=continuation.last_error,
        agent_bus_payload=payload,
        lifecycle_events=["agent_bus_dispatch_completed"] if success and agent_bus_attempted else [],
        continuation_id=continuation.continuation_id,
        continuation_status=continuation.status.value,
        idempotency_key=continuation.idempotency_key,
    )


def _workflow_chain_title(continuation: dict[str, Any]) -> str:
    return (
        f"{continuation['next_workflow_step']}: continue PR #{continuation['pr_number']} "
        f"on {continuation['branch']}"
    )


def _workflow_chain_log_context(item: Any) -> dict[str, Any]:
    context = workflow_chain_context_from_item(item) or {}
    return {
        "workflow_chain_id": context.get("workflow_chain_id"),
        "current_workflow_step": context.get("workflow_step"),
        "next_workflow_step": context.get("next_workflow_step"),
        "work_item_id": context.get("previous_work_item_id"),
        "pr_number": context.get("pr_number"),
        "branch": context.get("branch"),
        "status": "FINAL_STEP",
    }


def _workflow_steps_from_context(*contexts: dict[str, Any]) -> list[str] | None:
    for context in contexts:
        steps = _normalize_workflow_sequence(
            _first_present(
                context.get("workflow_steps"),
                context.get("workflowSteps"),
                context.get("workflow_sequence"),
                context.get("workflowSequence"),
            )
        )
        if steps:
            return steps
    return None


def _normalize_workflow_sequence(value: Any) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        raw_steps = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        raw_steps = [str(part).strip() for part in value]
    else:
        return None
    steps = [_normalize_workflow_step(step) for step in raw_steps if step]
    normalized = [step for step in steps if step]
    return normalized or None


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


def _normalize_workflow_step(value: Any) -> str | None:
    text = _string_or_none(value)
    if text is None:
        return None
    normalized = text.upper()
    if normalized.startswith("WF") and normalized[2:].isdigit():
        return normalized
    if normalized.isdigit():
        return f"WF{normalized}"
    return text


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
