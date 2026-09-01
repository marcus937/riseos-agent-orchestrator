import json
import sqlite3
from datetime import UTC, datetime, timedelta

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


def test_bounded_sqlite_agent_task_collection_refreshes_dependency_state(tmp_path) -> None:
    db_path = tmp_path / "agent_tasks.db"
    store = SQLiteAgentTaskStore(str(db_path))
    task_a = create_agent_task(request("Task A"))
    task_b = create_agent_task(request("Task B", dependency_task_ids=[task_a.task_id]))
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    task_a.created_at = base
    task_a.updated_at = base
    task_b.created_at = base + timedelta(minutes=1)
    task_b.updated_at = task_b.created_at
    store.save_agent_task(task_a)
    store.save_agent_task(task_b)

    first_page = SQLiteAgentTaskStore(str(db_path)).list_agent_tasks_for_workflow_collection(
        limit=1,
        workflow_filter="all",
    )

    assert [task.task_id for task in first_page] == [task_b.task_id]
    assert first_page[0].blocked is True
    assert first_page[0].blocked_by == [task_a.task_id]

    task_a.status = AgentTaskStatus.COMPLETED
    task_a.completed_at = task_a.updated_at
    store.save_agent_task(task_a)

    refreshed_page = SQLiteAgentTaskStore(str(db_path)).list_agent_tasks_for_workflow_collection(
        limit=1,
        workflow_filter="all",
    )

    assert [task.task_id for task in refreshed_page] == [task_b.task_id]
    assert refreshed_page[0].blocked is False
    assert refreshed_page[0].blocked_by == []


def test_sqlite_agent_task_store_adds_last_actor_to_existing_tables(tmp_path) -> None:
    db_path = tmp_path / "agent_tasks.db"
    now = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    lifecycle_events = [
        {"event": "created", "occurred_at": now.isoformat(), "actor": "orchestrator", "metadata": {}},
        {"event": "queued", "occurred_at": now.isoformat(), "actor": "orchestrator", "metadata": {}},
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE agent_tasks (
                task_id TEXT PRIMARY KEY,
                repo_full_name TEXT NOT NULL,
                title TEXT NOT NULL,
                objective TEXT NOT NULL,
                body TEXT,
                labels TEXT NOT NULL DEFAULT '[]',
                instructions TEXT NOT NULL,
                acceptance_criteria TEXT NOT NULL,
                target_agent TEXT NOT NULL,
                priority TEXT NOT NULL,
                correlation_id TEXT,
                dependency_task_ids TEXT NOT NULL DEFAULT '[]',
                blocked INTEGER NOT NULL DEFAULT 0,
                blocked_by TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                issue_number INTEGER,
                agent_bus_work_item_id TEXT,
                agent_bus_dispatch_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                queued_at TEXT,
                assigned_at TEXT,
                claimed_at TEXT,
                running_at TEXT,
                completed_at TEXT,
                failed_at TEXT,
                cancelled_at TEXT,
                branch TEXT,
                commit_sha TEXT,
                changed_files TEXT NOT NULL DEFAULT '[]',
                execution_evidence TEXT NOT NULL DEFAULT '{}',
                lifecycle_events TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO agent_tasks (
                task_id,
                repo_full_name,
                title,
                objective,
                body,
                labels,
                instructions,
                acceptance_criteria,
                target_agent,
                priority,
                correlation_id,
                dependency_task_ids,
                blocked,
                blocked_by,
                status,
                source,
                issue_number,
                agent_bus_work_item_id,
                agent_bus_dispatch_error,
                created_at,
                updated_at,
                queued_at,
                assigned_at,
                claimed_at,
                running_at,
                completed_at,
                failed_at,
                cancelled_at,
                branch,
                commit_sha,
                changed_files,
                execution_evidence,
                lifecycle_events
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                "legacy-agent",
                "riseos/example",
                "Legacy agent task",
                "Exercise additive migration.",
                None,
                "[]",
                json.dumps(["Do the work."]),
                json.dumps(["It works."]),
                "codex-m2",
                "normal",
                "workflow-legacy",
                "[]",
                0,
                "[]",
                "queued",
                "direct_api",
                17,
                None,
                None,
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
                None,
                None,
                None,
                None,
                None,
                None,
                "agent-integration",
                "abc123",
                "[]",
                "{}",
                json.dumps(lifecycle_events),
            ),
        )

    store = SQLiteAgentTaskStore(str(db_path))
    with sqlite3.connect(db_path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(agent_tasks)").fetchall()}
    assert "last_actor" in columns

    legacy = store.get_agent_task("legacy-agent")
    summaries = store.list_agent_task_workflow_summaries_for_collection(
        limit=10,
        workflow_filter="all",
    )

    assert legacy is not None
    assert legacy.task_id == "legacy-agent"
    assert summaries[0].task_id == "legacy-agent"
    assert summaries[0].last_actor is None

    apply_execution_result(
        legacy,
        AgentTaskExecutionResult(agent_id="codex-m2", status=AgentTaskStatus.CLAIMED),
    )
    store.save_agent_task(legacy)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_actor FROM agent_tasks WHERE task_id = ?",
            ("legacy-agent",),
        ).fetchone()
    assert row[0] == "codex-m2"
