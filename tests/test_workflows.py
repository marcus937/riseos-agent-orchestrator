import json
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent_tasks import (
    AgentTask,
    AgentTaskCreateRequest,
    AgentTaskLifecycleEvent,
    AgentTaskStatus,
    SQLiteAgentTaskStore,
    agent_task_store,
    create_agent_task,
)
from app.circuit_runtime_validation_routes import register_circuit_runtime_validation_routes
from app.config import get_settings
from app.event_store import EventRecord, event_store
from app.github_events import GitHubEventType
from app.main import app
from app.review_queue import ReviewWorkItem, review_queue
from app.security import build_signature
from app.storage import SQLiteStateStore
from app.workflow_lifecycle import WorkflowState
from app.workflow_routes import register_workflow_routes
from app.workflows import (
    WORKFLOW_LIST_DEFAULT_LIMIT,
    WORKFLOW_LIST_DEFAULT_RECENT_DAYS,
    WORKFLOW_LIST_MAX_LIMIT,
    WorkflowListFilter,
    WorkflowRecord,
    build_workflow_collection,
)


def client_with_secret(
    secret: str = "test-secret",
    admin_token: str = "admin-token",
    require_debug_read_token: bool = False,
    db_path: str | None = None,
) -> TestClient:
    get_settings.cache_clear()
    event_store.reset()
    review_queue.reset()
    agent_task_store.reset()
    for state_key in ("storage", "agent_task_store", "workflow_v1_store"):
        if hasattr(app.state, state_key):
            delattr(app.state, state_key)
    app.dependency_overrides[get_settings] = lambda: get_settings().__class__(
        github_webhook_secret=secret,
        orchestrator_db_path=db_path,
        orchestrator_admin_token=admin_token,
        require_admin_token_for_debug_reads=require_debug_read_token,
        hermes_m2_token="hermes-m2-secret",
        hermes_dgx_token="hermes-dgx-secret",
    )
    return TestClient(app)


class NoFullWorkflowListSQLiteStateStore(SQLiteStateStore):
    def list_review_work_items(self) -> list[ReviewWorkItem]:
        raise AssertionError("GET /api/v1/workflows must not hydrate all review work items")

    def recent_events(self, limit: int = 50) -> list[EventRecord]:
        raise AssertionError("GET /api/v1/workflows must not hydrate legacy recent events")


class NoFullWorkflowListSQLiteAgentTaskStore(SQLiteAgentTaskStore):
    def list_agent_tasks(self) -> list[AgentTask]:
        raise AssertionError("GET /api/v1/workflows must not hydrate all agent tasks")


def signed_headers(secret: str, event: str, payload: bytes) -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": build_signature(secret, payload),
        "Content-Type": "application/json",
    }


def _post_agent_integration_push(client: TestClient, secret: str = "test-secret") -> None:
    payload = {
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "agent"},
        "ref": "refs/heads/agent-integration",
        "after": "abc123",
    }
    body = json.dumps(payload).encode("utf-8")
    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "push", body))
    assert response.status_code == 200


def _post_pull_request_event(client: TestClient, action: str, *, merged: bool, secret: str = "test-secret") -> None:
    payload = {
        "action": action,
        "number": 17,
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "human"},
        "pull_request": {
            "number": 17,
            "merged": merged,
            "head": {
                "sha": "def456",
                "ref": "agent-integration",
                "repo": {"full_name": "riseos/example"},
            },
            "base": {
                "ref": "main",
                "repo": {"full_name": "riseos/example"},
            },
            "labels": [],
        },
    }
    body = json.dumps(payload).encode("utf-8")
    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "pull_request", body))
    assert response.status_code == 200


def _route_paths(test_app: FastAPI) -> set[str]:
    paths: set[str] = set()
    for route in test_app.routes:
        path = getattr(route, "path", None)
        if path:
            paths.add(str(path))
        for child in getattr(route, "routes", []):
            child_path = getattr(child, "path", None)
            if child_path:
                paths.add(str(child_path))
    return paths


def test_workflow_routes_are_registered_from_application_composition() -> None:
    runtime_only_app = FastAPI()
    register_circuit_runtime_validation_routes(runtime_only_app)
    assert any(path.startswith("/api/v1/runtime-validations") for path in _route_paths(runtime_only_app))
    assert not any(path.startswith("/api/v1/workflows") for path in _route_paths(runtime_only_app))

    composed_app = FastAPI()
    register_workflow_routes(composed_app)
    register_circuit_runtime_validation_routes(composed_app)
    assert "/api/v1/workflows" in _route_paths(composed_app)
    assert any(path.startswith("/api/v1/runtime-validations") for path in _route_paths(composed_app))


def test_workflow_endpoints_return_canonical_record_and_timeline() -> None:
    client = client_with_secret()
    _post_agent_integration_push(client)

    collection = client.get("/api/v1/workflows")

    assert collection.status_code == 200
    workflows = collection.json()["workflows"]
    assert len(workflows) == 1
    workflow = workflows[0]
    assert workflow["workflow_id"].startswith("wf-")
    assert workflow["repo_full_name"] == "riseos/example"
    assert workflow["current_state"] == "CIRCUIT_WORKING"
    assert workflow["last_actor"] == "Circuit"
    assert "timeline" not in workflow
    assert "route_history" not in workflow

    detail = client.get(f"/api/v1/workflows/{workflow['workflow_id']}")
    timeline = client.get(f"/api/v1/workflows/{workflow['workflow_id']}/timeline")

    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["workflow_id"] == workflow["workflow_id"]
    assert detail_body["timeline"][0]["event_type"] == "workflow.lifecycle.changed"
    assert detail_body["timeline"][0]["state"] == "CIRCUIT_IN_PROGRESS"
    assert detail_body["timeline"][0]["canonical_state"] == "CIRCUIT_WORKING"
    assert detail_body["timeline"][0]["new_state"] == "CIRCUIT_WORKING"
    assert detail_body["route_history"] == ["Circuit: CIRCUIT_WORKING"]
    assert timeline.status_code == 200
    assert timeline.json()["events"][0]["new_state"] == "CIRCUIT_WORKING"


def test_workflow_collection_defaults_to_bounded_active_recent_page() -> None:
    now = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    active_workflows = [
        _workflow_record(
            f"wf-active-{index:02d}",
            WorkflowState.ASSIGNED,
            now - timedelta(minutes=index + 1),
        )
        for index in range(WORKFLOW_LIST_DEFAULT_LIMIT + 5)
    ]
    recent_terminal = _workflow_record("wf-terminal-recent", WorkflowState.MERGED, now - timedelta(seconds=30))
    stale_terminal = _workflow_record(
        "wf-terminal-stale",
        WorkflowState.MERGED,
        now - timedelta(days=WORKFLOW_LIST_DEFAULT_RECENT_DAYS + 1),
    )

    collection = build_workflow_collection(
        [*active_workflows, recent_terminal, stale_terminal],
        now=now,
    )

    workflow_ids = [workflow.workflow_id for workflow in collection.workflows]
    assert len(workflow_ids) == WORKFLOW_LIST_DEFAULT_LIMIT
    assert workflow_ids[0] == "wf-terminal-recent"
    assert "wf-terminal-stale" not in workflow_ids
    assert collection.pagination is not None
    assert collection.pagination.filter == WorkflowListFilter.ACTIVE_RECENT
    assert collection.pagination.returned == WORKFLOW_LIST_DEFAULT_LIMIT
    assert collection.pagination.total == WORKFLOW_LIST_DEFAULT_LIMIT + 6
    assert collection.pagination.unfiltered_total == WORKFLOW_LIST_DEFAULT_LIMIT + 7
    assert collection.pagination.truncated is True
    assert collection.pagination.has_next is True
    assert collection.pagination.next_offset == WORKFLOW_LIST_DEFAULT_LIMIT


def test_workflow_collection_uses_stable_id_tiebreaker_for_equal_activity() -> None:
    now = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    collection = build_workflow_collection(
        [
            _workflow_record("wf-charlie", WorkflowState.ASSIGNED, now),
            _workflow_record("wf-alpha", WorkflowState.ASSIGNED, now),
            _workflow_record("wf-bravo", WorkflowState.ASSIGNED, now),
        ],
        workflow_filter=WorkflowListFilter.ALL,
        now=now,
    )

    assert [workflow.workflow_id for workflow in collection.workflows] == ["wf-alpha", "wf-bravo", "wf-charlie"]


def test_workflow_endpoint_honors_pagination_query_params() -> None:
    client = client_with_secret()
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    review_queue.add_if_absent(_review_item("old", base))
    review_queue.add_if_absent(_review_item("middle", base + timedelta(minutes=1)))
    review_queue.add_if_absent(_review_item("new", base + timedelta(minutes=2)))

    response = client.get("/api/v1/workflows?filter=all&limit=2&offset=1")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == ["wf-middle", "wf-old"]
    assert body["pagination"] == {
        "limit": 2,
        "offset": 1,
        "returned": 2,
        "total": 3,
        "unfiltered_total": 3,
        "truncated": False,
        "has_next": False,
        "next_offset": None,
        "filter": "all",
        "recent_days": WORKFLOW_LIST_DEFAULT_RECENT_DAYS,
    }


def test_workflow_endpoint_uses_bounded_sqlite_queries_for_collection(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)

    for index in range(8):
        storage.save_review_work_item(_review_item(f"review-{index}", base + timedelta(minutes=index)))
    for index in range(8):
        task = create_agent_task(
            AgentTaskCreateRequest(
                repo_full_name="riseos/example",
                title=f"Task {index}",
                objective=f"Run task {index}.",
            )
        )
        task.task_id = f"bounded-{index}"
        task.created_at = base + timedelta(minutes=20 + index)
        task.updated_at = task.created_at
        for event in task.lifecycle_events:
            event.occurred_at = task.created_at
        task_store.save_agent_task(task)
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=all&limit=3")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-agent-task-bounded-7",
        "wf-agent-task-bounded-6",
        "wf-agent-task-bounded-5",
    ]
    assert body["pagination"] == {
        "limit": 3,
        "offset": 0,
        "returned": 3,
        "total": 16,
        "unfiltered_total": 16,
        "truncated": True,
        "has_next": True,
        "next_offset": 3,
        "filter": "all",
        "recent_days": WORKFLOW_LIST_DEFAULT_RECENT_DAYS,
    }


def test_workflow_endpoint_bounded_sqlite_recent_filter_counts_review_items(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    now = datetime.now(UTC)

    for index in range(5):
        storage.save_review_work_item(
            _review_item(f"stale-review-{index}", now - timedelta(days=30, minutes=index))
        )
    for index in range(2):
        storage.save_review_work_item(
            _review_item(f"recent-review-{index}", now - timedelta(minutes=index))
        )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=recent&limit=10&recent_days=14")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-recent-review-0",
        "wf-recent-review-1",
    ]
    assert body["pagination"]["total"] == 2
    assert body["pagination"]["unfiltered_total"] == 7
    assert body["pagination"]["has_next"] is False


def test_workflow_endpoint_bounded_sqlite_active_filter_skips_terminal_agent_tasks_before_limit(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    now = datetime.now(UTC)

    for index in range(6):
        task_store.save_agent_task(
            _agent_task(
                f"completed-{index}",
                AgentTaskStatus.COMPLETED,
                now - timedelta(minutes=index),
            )
        )
    for index in range(4):
        task_store.save_agent_task(
            _agent_task(
                f"active-{index}",
                AgentTaskStatus.QUEUED,
                now - timedelta(hours=1, minutes=index),
            )
        )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=active&limit=3")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-agent-task-active-0",
        "wf-agent-task-active-1",
        "wf-agent-task-active-2",
    ]
    assert body["pagination"]["returned"] == 3
    assert body["pagination"]["total"] == 4
    assert body["pagination"]["unfiltered_total"] == 10
    assert body["pagination"]["has_next"] is True
    assert body["pagination"]["next_offset"] == 3


def test_workflow_endpoint_bounded_sqlite_event_candidates_follow_requested_page_size(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)

    for index in range(120):
        storage.save_event_record(
            EventRecord(
                event_id=f"event-{index}",
                github_event=GitHubEventType.PUSH,
                repo_full_name="riseos/example",
                branch=f"branch-{index}",
                commit_sha=f"sha-{index}",
                received_at=base + timedelta(minutes=index),
            )
        )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=all&limit=75")

    assert response.status_code == 200
    body = response.json()
    assert len(body["workflows"]) == 75
    assert body["workflows"][0]["workflow_id"] == "wf-event-119"
    assert body["workflows"][-1]["workflow_id"] == "wf-event-45"
    assert body["pagination"]["returned"] == 75
    assert body["pagination"]["total"] == 120
    assert body["pagination"]["has_next"] is True
    assert body["pagination"]["next_offset"] == 75


def test_workflow_detail_uses_targeted_sqlite_review_item_lookup(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    now = datetime.now(UTC)
    storage.save_review_work_item(_review_item("detail-review", now))
    app.state.storage = storage
    app.state.agent_task_store = task_store

    detail = client.get("/api/v1/workflows/wf-detail-review")
    timeline = client.get("/api/v1/workflows/wf-detail-review/timeline")

    assert detail.status_code == 200
    assert detail.json()["workflow_id"] == "wf-detail-review"
    assert detail.json()["timeline"][0]["item_id"] == "detail-review"
    assert timeline.status_code == 200
    assert timeline.json()["events"][0]["item_id"] == "detail-review"


def test_workflow_detail_uses_targeted_sqlite_agent_task_lookup(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    now = datetime.now(UTC)
    task_store.save_agent_task(_agent_task("detail-agent", AgentTaskStatus.QUEUED, now))
    app.state.storage = storage
    app.state.agent_task_store = task_store

    detail = client.get("/api/v1/workflows/wf-agent-task-detail-agent")
    timeline = client.get("/api/v1/workflows/wf-agent-task-detail-agent/timeline")

    assert detail.status_code == 200
    assert detail.json()["workflow_id"] == "wf-agent-task-detail-agent"
    assert detail.json()["agent_task_id"] == "detail-agent"
    assert detail.json()["timeline"][0]["item_id"] == "detail-agent"
    assert timeline.status_code == 200
    assert timeline.json()["events"][0]["item_id"] == "detail-agent"


def test_workflow_detail_uses_targeted_sqlite_event_lookup_by_correlation_id(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    target = EventRecord(
        event_id="target-event",
        github_event=GitHubEventType.PUSH,
        correlation_key="riseos/example:commit:target-sha",
        repo_full_name="riseos/example",
        branch="agent-target",
        commit_sha="target-sha",
        received_at=base,
    )
    storage.save_event_record(target)
    for index in range(60):
        storage.save_event_record(
            EventRecord(
                event_id=f"newer-event-{index}",
                github_event=GitHubEventType.PUSH,
                repo_full_name="riseos/example",
                branch=f"agent-newer-{index}",
                commit_sha=f"newer-sha-{index}",
                received_at=base + timedelta(minutes=index + 1),
            )
        )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    workflow_id = f"wf-{target.correlation_id}"
    detail = client.get(f"/api/v1/workflows/{workflow_id}")
    timeline = client.get(f"/api/v1/workflows/{workflow_id}/timeline")

    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["workflow_id"] == workflow_id
    assert detail_body["correlation_id"] == target.correlation_id
    assert detail_body["timeline"][0]["source"] == "github_webhook"
    assert detail_body["timeline"][0]["commit_sha"] == "target-sha"
    assert timeline.status_code == 200
    assert timeline.json()["events"][0]["commit_sha"] == "target-sha"


def test_workflow_endpoint_bounded_sqlite_event_filters_match_active_recent_contract(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    now = datetime.now(UTC)

    storage.save_event_record(
        EventRecord(
            event_id="stale-active-push",
            github_event=GitHubEventType.PUSH,
            repo_full_name="riseos/example",
            branch="agent-stale-active",
            commit_sha="sha-stale-active",
            received_at=now - timedelta(days=30),
        )
    )
    storage.save_event_record(
        EventRecord(
            event_id="recent-terminal-pr",
            github_event=GitHubEventType.PULL_REQUEST,
            repo_full_name="riseos/example",
            pr_number=17,
            pr_merged=True,
            received_at=now - timedelta(minutes=5),
            raw_action="closed",
        )
    )
    storage.save_event_record(
        EventRecord(
            event_id="stale-terminal-pr",
            github_event=GitHubEventType.PULL_REQUEST,
            repo_full_name="riseos/example",
            pr_number=18,
            pr_merged=False,
            received_at=now - timedelta(days=30),
            raw_action="closed",
        )
    )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=active_recent&limit=10&recent_days=14")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-recent-terminal-pr",
        "wf-stale-active-push",
    ]
    assert body["pagination"]["total"] == 2
    assert body["pagination"]["unfiltered_total"] == 3


def test_workflow_endpoint_rejects_unbounded_limit() -> None:
    client = client_with_secret()

    response = client.get(f"/api/v1/workflows?limit={WORKFLOW_LIST_MAX_LIMIT + 1}")

    assert response.status_code == 422


def test_workflow_endpoints_use_debug_read_access_policy() -> None:
    client = client_with_secret(require_debug_read_token=True)

    assert client.get("/api/v1/workflows").status_code == 401
    response = client.get("/api/v1/workflows", headers={"X-Orchestrator-Admin-Token": "admin-token"})

    assert response.status_code == 200
    assert response.json()["workflows"] == []


def test_pull_request_closed_is_not_automatically_merged() -> None:
    client = client_with_secret()
    _post_pull_request_event(client, "closed", merged=False)

    collection = client.get("/api/v1/workflows")

    assert collection.status_code == 200
    workflows = collection.json()["workflows"]
    assert workflows[0]["current_state"] == "CLOSED_UNMERGED"
    assert "timeline" not in workflows[0]

    detail = client.get(f"/api/v1/workflows/{workflows[0]['workflow_id']}")
    assert detail.status_code == 200
    assert detail.json()["timeline"][0]["state"] == "CLOSED_UNMERGED"


def test_pull_request_closed_merged_is_explicitly_merged() -> None:
    client = client_with_secret()
    _post_pull_request_event(client, "closed", merged=True)

    collection = client.get("/api/v1/workflows")

    assert collection.status_code == 200
    workflows = collection.json()["workflows"]
    assert workflows[0]["current_state"] == "MERGED"
    assert "timeline" not in workflows[0]

    detail = client.get(f"/api/v1/workflows/{workflows[0]['workflow_id']}")
    assert detail.status_code == 200
    assert detail.json()["timeline"][0]["state"] == "MERGED"


def _workflow_record(workflow_id: str, state: WorkflowState, last_activity_at: datetime) -> WorkflowRecord:
    return WorkflowRecord(
        workflow_id=workflow_id,
        current_state=state,
        last_actor="Orchestrator",
        created_at=last_activity_at,
        updated_at=last_activity_at,
        last_activity_at=last_activity_at,
    )


def _review_item(item_id: str, created_at: datetime) -> ReviewWorkItem:
    return ReviewWorkItem(
        id=item_id,
        created_at=created_at,
        updated_at=created_at,
        repo_full_name="riseos/example",
        event_type=GitHubEventType.PUSH,
        branch=f"agent-{item_id}",
        commit_sha=f"sha-{item_id}",
    )


def _agent_task(task_id: str, status: AgentTaskStatus, activity_at: datetime) -> AgentTask:
    return AgentTask(
        task_id=task_id,
        repo_full_name="riseos/example",
        title=f"Task {task_id}",
        objective=f"Run task {task_id}.",
        target_agent="codex-m2",
        status=status,
        created_at=activity_at,
        updated_at=activity_at,
        queued_at=activity_at if status == AgentTaskStatus.QUEUED else None,
        completed_at=activity_at if status == AgentTaskStatus.COMPLETED else None,
        lifecycle_events=[
            AgentTaskLifecycleEvent(event="created", occurred_at=activity_at),
            AgentTaskLifecycleEvent(event=status.value, occurred_at=activity_at),
        ],
    )
