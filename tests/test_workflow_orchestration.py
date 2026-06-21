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


def _create_linear_workflow() -> tuple[InMemoryAgentTaskStore, object, object]:
    agent_store = InMemoryAgentTaskStore()
    workflow_store = InMemoryWorkflowStore()
    workflow = create_workflow(_linear_workflow_request(), workflow_store=workflow_store, agent_task_store=agent_store)
    return agent_store, workflow_store, workflow


def _complete_task(agent_store: InMemoryAgentTaskStore, task_id: str, *, key: str) -> None:
    task = agent_store.get_agent_task(task_id)
    apply_execution_result(
        task,
        AgentTaskExecutionResult(
            agent_id="codex-m2",
            status=AgentTaskStatus.COMPLETED,
            commit_sha=f"{key.lower()}bc123",
            branch=f"codex-m2/{key.lower()}",
            changed_files=[f"docs/{key.lower()}.md"],
            evidence={"summary": f"{key} complete"},
        ),
    )
    agent_store.save_agent_task(task)


def _fail_task(agent_store: InMemoryAgentTaskStore, task_id: str, *, key: str) -> None:
    task = agent_store.get_agent_task(task_id)
    apply_execution_result(
        task,
        AgentTaskExecutionResult(
            agent_id="codex-m2",
            status=AgentTaskStatus.FAILED,
            branch=f"codex-m2/{key.lower()}",
            changed_files=[f"docs/{key.lower()}.md"],
            evidence={"error": f"{key} failed"},
        ),
    )
    agent_store.save_agent_task(task)


def test_create_workflow_persists_dependency_graph_without_workflow_blocking() -> None:
    agent_store, _, workflow = _create_linear_workflow()

    response = build_workflow_response(workflow, agent_store.list_agent_tasks())

    task_by_key = {task.task_key: task for task in response.task_statuses}
    assert response.workflow_id == workflow.workflow_id
    assert response.status == WorkflowStatus.READY
    assert task_by_key["A"].blocked is False
    assert task_by_key["B"].blocked is True
    assert task_by_key["B"].blocked_by == [task_by_key["A"].task_id]
    assert task_by_key["C"].blocked is True
    assert response.dependency_graph[1].depends_on == ["A"]


def test_release_runnable_tasks_advances_dependency_chain() -> None:
    agent_store, _, workflow = _create_linear_workflow()
    client = FakeAgentBusClient()

    released = run(release_runnable_agent_tasks(agent_store, client, review_agent="bb2"))

    assert [task.title for task in released] == ["Step A"]
    assert client.payloads[0]["metadata"]["workflow_id"] == workflow.workflow_id

    response = build_workflow_response(workflow, agent_store.list_agent_tasks())
    task_by_key = {task.task_key: task for task in response.task_statuses}
    _complete_task(agent_store, task_by_key["A"].task_id, key="A")

    released = run(release_runnable_agent_tasks(agent_store, client, review_agent="bb2"))

    assert [task.title for task in released] == ["Step B"]
    response = build_workflow_response(workflow, agent_store.list_agent_tasks())
    task_by_key = {task.task_key: task for task in response.task_statuses}
    assert response.status == WorkflowStatus.RUNNING
    assert response.current_running_task.task_key == "B"
    assert response.completed_tasks == ["A"]
    assert response.task_results["A"] == {"summary": "A complete"}
    assert task_by_key["B"].blocked is False
    assert task_by_key["C"].blocked is True


def test_workflow_reports_completed_after_all_tasks_complete() -> None:
    agent_store, _, workflow = _create_linear_workflow()
    response = build_workflow_response(workflow, agent_store.list_agent_tasks())
    task_by_key = {task.task_key: task for task in response.task_statuses}

    _complete_task(agent_store, task_by_key["A"].task_id, key="A")
    response = build_workflow_response(workflow, agent_store.list_agent_tasks())
    task_by_key = {task.task_key: task for task in response.task_statuses}
    _complete_task(agent_store, task_by_key["B"].task_id, key="B")
    response = build_workflow_response(workflow, agent_store.list_agent_tasks())
    task_by_key = {task.task_key: task for task in response.task_statuses}
    _complete_task(agent_store, task_by_key["C"].task_id, key="C")

    response = build_workflow_response(workflow, agent_store.list_agent_tasks())

    assert response.status == WorkflowStatus.COMPLETED
    assert response.completed_tasks == ["A", "B", "C"]


def test_workflow_reports_failed_when_dependency_fails_and_no_runnable_tasks_remain() -> None:
    agent_store, _, workflow = _create_linear_workflow()
    response = build_workflow_response(workflow, agent_store.list_agent_tasks())
    task_by_key = {task.task_key: task for task in response.task_statuses}

    _fail_task(agent_store, task_by_key["A"].task_id, key="A")

    response = build_workflow_response(workflow, agent_store.list_agent_tasks())
    task_by_key = {task.task_key: task for task in response.task_statuses}

    assert response.status == WorkflowStatus.FAILED
    assert response.failures == ["A failed"]
    assert task_by_key["B"].blocked is True
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
