from app.agent_tasks import (
    AgentTask,
    AgentTaskCreateRequest,
    AgentTaskExecutionResult,
    AgentTaskStatus,
    InMemoryAgentTaskStore,
    SQLiteAgentTaskStore,
    apply_execution_result,
    create_agent_task,
    missing_dependency_task_ids,
    refresh_agent_task_dependency_state,
    refresh_agent_task_dependency_states,
)


def request(title: str, *, dependency_task_ids: list[str] | None = None) -> AgentTaskCreateRequest:
    return AgentTaskCreateRequest(
        repo_full_name="riseos/example",
        title=title,
        body=f"Body for {title}",
        dependency_task_ids=dependency_task_ids or [],
    )


def test_agent_task_schema_exposes_dependency_fields() -> None:
    create_schema = AgentTaskCreateRequest.model_json_schema()
    response_schema = AgentTask.model_json_schema()

    assert "dependency_task_ids" in create_schema["properties"]
    assert "dependency_task_ids" in response_schema["properties"]
    assert "blocked" in response_schema["properties"]
    assert "blocked_by" in response_schema["properties"]


def test_dependent_task_is_blocked_until_dependency_completes() -> None:
    task_a = create_agent_task(request("Task A"))
    task_b = create_agent_task(request("Task B", dependency_task_ids=[task_a.task_id]))

    refresh_agent_task_dependency_states([task_a, task_b])

    assert task_a.blocked is False
    assert task_b.blocked is True
    assert task_b.blocked_by == [task_a.task_id]

    apply_execution_result(task_a, AgentTaskExecutionResult(agent_id="codex-m2", status=AgentTaskStatus.COMPLETED))
    refresh_agent_task_dependency_states([task_a, task_b])

    assert task_b.blocked is False
    assert task_b.blocked_by == []


def test_multiple_dependencies_require_all_completed() -> None:
    task_a = create_agent_task(request("Task A"))
    task_b = create_agent_task(request("Task B"))
    task_c = create_agent_task(request("Task C"))
    task_d = create_agent_task(request("Task D", dependency_task_ids=[task_a.task_id, task_b.task_id, task_c.task_id]))

    apply_execution_result(task_a, AgentTaskExecutionResult(agent_id="codex-m2", status=AgentTaskStatus.COMPLETED))
    apply_execution_result(task_b, AgentTaskExecutionResult(agent_id="codex-m2", status=AgentTaskStatus.COMPLETED))
    refresh_agent_task_dependency_states([task_a, task_b, task_c, task_d])

    assert task_d.blocked is True
    assert task_d.blocked_by == [task_c.task_id]

    apply_execution_result(task_c, AgentTaskExecutionResult(agent_id="codex-m2", status=AgentTaskStatus.COMPLETED))
    refresh_agent_task_dependency_states([task_a, task_b, task_c, task_d])

    assert task_d.blocked is False
    assert task_d.blocked_by == []


def test_invalid_dependency_task_id_is_reported() -> None:
    store = InMemoryAgentTaskStore()
    task_a = create_agent_task(request("Task A"))
    store.save_agent_task(task_a)

    assert missing_dependency_task_ids([task_a.task_id, "agtask-missing"], store) == ["agtask-missing"]


def test_dependency_task_ids_survive_sqlite_reload(tmp_path) -> None:
    db_path = tmp_path / "agent_tasks.db"
    store = SQLiteAgentTaskStore(str(db_path))
    task_a = create_agent_task(request("Task A"))
    task_b = create_agent_task(request("Task B", dependency_task_ids=[task_a.task_id]))
    refresh_agent_task_dependency_state(task_b, {task_a.task_id: task_a})
    store.save_agent_task(task_a)
    store.save_agent_task(task_b)

    reloaded = SQLiteAgentTaskStore(str(db_path))
    saved_b = reloaded.get_agent_task(task_b.task_id)

    assert saved_b is not None
    assert saved_b.dependency_task_ids == [task_a.task_id]
    assert saved_b.blocked is True
    assert saved_b.blocked_by == [task_a.task_id]
