import asyncio

from app.agent_task_release import release_runnable_agent_tasks
from app.agent_tasks import AgentTaskExecutionResult, AgentTaskStatus, InMemoryAgentTaskStore, apply_execution_result
from app.workflow_orchestration import (
    InMemoryWorkflowStore,
    WorkflowCreateRequest,
    WorkflowPRStrategy,
    WorkflowTask,
    build_workflow_response,
    create_workflow,
    update_shared_workflow_routing_after_result,
)


def run(coro):
    return asyncio.run(coro)


class FakeAgentBusClient:
    def __init__(self) -> None:
        self.payloads = []

    async def create_work_item(self, payload):
        self.payloads.append(payload)
        return {"work_item_id": f"wi-{len(self.payloads)}"}


def _request(strategy: WorkflowPRStrategy) -> WorkflowCreateRequest:
    return WorkflowCreateRequest(
        repo_full_name="marcus937/jarvis-mission-control",
        title="Shared PR chain",
        pr_strategy=strategy,
        tasks=[
            WorkflowTask(task_key="A", title="Step A", objective="Create docs/a.md"),
            WorkflowTask(task_key="B", title="Step B", objective="Create docs/b.md", depends_on=["A"]),
        ],
    )


def _complete_with_pr(agent_store: InMemoryAgentTaskStore, task_id: str, branch: str, pr_number: int) -> None:
    task = agent_store.get_agent_task(task_id)
    apply_execution_result(
        task,
        AgentTaskExecutionResult(
            agent_id="codex-m2",
            status=AgentTaskStatus.COMPLETED,
            commit_sha="abc123",
            branch=branch,
            changed_files=["docs/a.md"],
            evidence={"summary": "A complete", "pull_request": {"number": pr_number}},
        ),
    )
    agent_store.save_agent_task(task)


def test_per_task_pr_strategy_does_not_emit_shared_branch_metadata() -> None:
    agent_store = InMemoryAgentTaskStore()
    workflow_store = InMemoryWorkflowStore()
    workflow = create_workflow(_request(WorkflowPRStrategy.PER_TASK), workflow_store=workflow_store, agent_task_store=agent_store)
    client = FakeAgentBusClient()

    released = run(release_runnable_agent_tasks(agent_store, client, review_agent="bb2"))

    assert [task.title for task in released] == ["Step A"]
    assert client.payloads[0]["metadata"]["pr_strategy"] == "per_task"
    assert "source_branch" not in client.payloads[0]["metadata"]
    response = build_workflow_response(workflow, agent_store.list_agent_tasks())
    assert response.pr_strategy == WorkflowPRStrategy.PER_TASK
    assert response.shared_branch is None
    assert response.shared_pr_number is None


def test_shared_workflow_branch_reuses_branch_and_pr_for_downstream_task() -> None:
    agent_store = InMemoryAgentTaskStore()
    workflow_store = InMemoryWorkflowStore()
    workflow = create_workflow(_request(WorkflowPRStrategy.SHARED_WORKFLOW_BRANCH), workflow_store=workflow_store, agent_task_store=agent_store)
    client = FakeAgentBusClient()

    run(release_runnable_agent_tasks(agent_store, client, review_agent="bb2"))

    assert client.payloads[0]["metadata"]["pr_strategy"] == "shared_workflow_branch"
    assert client.payloads[0]["metadata"]["source_branch"] == workflow.shared_branch
    assert "source_pr_number" not in client.payloads[0]["metadata"]

    response = build_workflow_response(workflow, agent_store.list_agent_tasks())
    task_by_key = {task.task_key: task for task in response.task_statuses}
    _complete_with_pr(agent_store, task_by_key["A"].task_id, workflow.shared_branch, 90)
    update_shared_workflow_routing_after_result(workflow, agent_store)
    workflow_store.save_workflow(workflow)

    run(release_runnable_agent_tasks(agent_store, client, review_agent="bb2"))

    assert client.payloads[1]["metadata"]["source_branch"] == workflow.shared_branch
    assert client.payloads[1]["metadata"]["source_pr_number"] == 90
    assert client.payloads[1]["pr_number"] == 90
    response = build_workflow_response(workflow, agent_store.list_agent_tasks())
    assert response.shared_branch == workflow.shared_branch
    assert response.shared_pr_number == 90
