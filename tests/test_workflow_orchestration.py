import asyncio

from app.agent_task_release import release_runnable_agent_tasks
from app.agent_tasks import AgentTaskExecutionResult, AgentTaskStatus, InMemoryAgentTaskStore, apply_execution_result
from app.workflow_orchestration import (
    InMemoryWorkflowStore,
    WorkflowCreateRequest,
    WorkflowStatus,
    WorkflowTask,
    build_workflow_response,
    create_workflow,
)


def run(coro):
    return asyncio.run(coro)


class FakeAgentBusClient:
    def __init__(self) -> None:
        self.payloads = []

    async def create_work_item(self, payload):
        self.payloads.append(payload)
        return {"work_item_id": f"wi-{len(self.payloads)}"}


def _linear_workflow_request() -> WorkflowCreateRequest:
    return WorkflowCreateRequest(
        repo_full_name="marcus937/jarvis-mission-control",
        title="Dependency chain",
        tasks=[
            WorkflowTask(task_key="A", title="Step A", objective="Create docs/a.md"),
            WorkflowTask(task_key="B", title="Step B", objective="Create docs/b.md", depends_on=["A"]),
            WorkflowTask(task_key="C", title="Step C", objective="Create docs/c.md", depends_on=["B"]),
        ],
    )


def test_create_workflow_persists_dependency_graph() -> None:
    agent_store = InMemoryAgentTaskStore()
    workflow_store = InMemoryWorkflowStore()

    workflow = create_workflow(_linear_workflow_request(), workflow_store=workflow_store, agent_task_store=agent_store)
    response = build_workflow_response(workflow, agent_store.list_agent_tasks())

    task_by_key = {task.task_key: task for task in response.task_statuses}
    assert response.workflow_id == workflow.workflow_id
    assert response.status == WorkflowStatus.BLOCKED
    assert task_by_key["A"].blocked is False
    assert task_by_key["B"].blocked is True
    assert task_by_key["B"].blocked_by == [task_by_key["A"].task_id]
    assert task_by_key["C"].blocked is True
    assert response.dependency_graph[1].depends_on == ["A"]


def test_release_runnable_tasks_advances_dependency_chain() -> None:
    agent_store = InMemoryAgentTaskStore()
    workflow_store = InMemoryWorkflowStore()
    workflow = create_workflow(_linear_workflow_request(), workflow_store=workflow_store, agent_task_store=agent_store)
    client = FakeAgentBusClient()

    released = run(release_runnable_agent_tasks(agent_store, client, review_agent="bb2"))

    assert [task.title for task in released] == ["Step A"]
    assert client.payloads[0]["metadata"]["workflow_id"].startswith("wf-agent-task-")

    response = build_workflow_response(workflow, agent_store.list_agent_tasks())
    task_by_key = {task.task_key: task for task in response.task_statuses}
    step_a = agent_store.get_agent_task(task_by_key["A"].task_id)
    apply_execution_result(
        step_a,
        AgentTaskExecutionResult(
            agent_id="codex-m2",
            status=AgentTaskStatus.COMPLETED,
            commit_sha="abc123",
            branch="codex-m2/a",
            changed_files=["docs/a.md"],
            evidence={"summary": "A complete"},
        ),
    )
    agent_store.save_agent_task(step_a)

    released = run(release_runnable_agent_tasks(agent_store, client, review_agent="bb2"))

    assert [task.title for task in released] == ["Step B"]
    response = build_workflow_response(workflow, agent_store.list_agent_tasks())
    task_by_key = {task.task_key: task for task in response.task_statuses}
    assert response.status == WorkflowStatus.RUNNING
    assert response.current_running_task.task_key == "B"
    assert response.completed_tasks == ["A"]
    assert response.task_results["A"] == {"summary": "A complete"}
    assert task_by_key["C"].blocked is True


def test_workflow_rejects_unknown_dependency() -> None:
    try:
        WorkflowCreateRequest(
            repo_full_name="marcus937/jarvis-mission-control",
            title="Bad workflow",
            tasks=[WorkflowTask(task_key="B", title="Step B", objective="Run B", depends_on=["A"])],
        )
    except ValueError as exc:
        assert "unknown task_key" in str(exc)
    else:
        raise AssertionError("WorkflowCreateRequest should reject unknown dependencies")
