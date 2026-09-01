import json
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent_tasks import (
    AgentTask,
    AgentTaskCreateRequest,
    AgentTaskLifecycleEvent,
    AgentTaskWorkflowSummary,
    AgentTaskStatus,
    SQLiteAgentTaskStore,
    agent_task_store,
    create_agent_task,
)
from app.circuit_runtime_validation_routes import register_circuit_runtime_validation_routes
from app.config import get_settings
from app.event_store import EventRecord, EventWorkflowSummary, event_store
from app.github_events import GitHubEventType
from app.main import app
from app.review_queue import ReviewWorkItem, ReviewWorkItemWorkflowSummary, review_queue
from app.security import build_signature
from app.storage import SQLiteStateStore
from app.workflow_lifecycle import WorkflowState
from app.workflow_routes import register_workflow_routes
from app.workflows import (
    WORKFLOW_LIST_DEFAULT_LIMIT,
    WORKFLOW_LIST_DEFAULT_RECENT_DAYS,
    WORKFLOW_LIST_MAX_LIMIT,
    WORKFLOW_LIST_MAX_OFFSET,
    WorkflowListFilter,
    WorkflowRecord,
    build_workflow_collection,
    build_workflows,
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


class SummaryOnlyWorkflowListSQLiteStateStore(NoFullWorkflowListSQLiteStateStore):
    def list_review_work_item_summary_records_for_workflow_collection(
        self,
        *args,
        **kwargs,
    ) -> list[ReviewWorkItemWorkflowSummary]:
        summaries = super().list_review_work_item_summary_records_for_workflow_collection(
            *args,
            **kwargs,
        )
        assert all(isinstance(summary, ReviewWorkItemWorkflowSummary) for summary in summaries)
        assert all(not hasattr(summary, "runtime_validation_context") for summary in summaries)
        assert all(not hasattr(summary, "agent_bus_dispatch_error") for summary in summaries)
        return summaries


class SummaryOnlyWorkflowListSQLiteAgentTaskStore(NoFullWorkflowListSQLiteAgentTaskStore):
    def list_agent_task_workflow_summaries_for_collection(self, *args, **kwargs) -> list[AgentTaskWorkflowSummary]:
        summaries = super().list_agent_task_workflow_summaries_for_collection(*args, **kwargs)
        assert all(isinstance(summary, AgentTaskWorkflowSummary) for summary in summaries)
        assert all(not hasattr(summary, "body") for summary in summaries)
        assert all(not hasattr(summary, "execution_evidence") for summary in summaries)
        assert all(not hasattr(summary, "lifecycle_events") for summary in summaries)
        return summaries


class QueryShapeWorkflowListSQLiteStateStore(NoFullWorkflowListSQLiteStateStore):
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self.review_summary_limits: list[int] = []
        self.event_summary_limits: list[int] = []

    def list_review_work_item_summary_records_for_workflow_collection(
        self,
        *,
        limit: int,
        workflow_filter: str = "active_recent",
        recent_since: datetime | None = None,
    ) -> list[ReviewWorkItemWorkflowSummary]:
        self.review_summary_limits.append(limit)
        return super().list_review_work_item_summary_records_for_workflow_collection(
            limit=limit,
            workflow_filter=workflow_filter,
            recent_since=recent_since,
        )

    def list_event_workflow_summary_records_for_collection(
        self,
        *,
        limit: int,
        workflow_filter: str = "active_recent",
        recent_since: datetime | None = None,
    ) -> list[EventWorkflowSummary]:
        self.event_summary_limits.append(limit)
        return super().list_event_workflow_summary_records_for_collection(
            limit=limit,
            workflow_filter=workflow_filter,
            recent_since=recent_since,
        )


class QueryShapeWorkflowListSQLiteAgentTaskStore(NoFullWorkflowListSQLiteAgentTaskStore):
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self.agent_summary_limits: list[int] = []

    def list_agent_task_workflow_summaries_for_collection(
        self,
        *,
        limit: int,
        workflow_filter: str = "active_recent",
        recent_since: datetime | None = None,
    ) -> list[AgentTaskWorkflowSummary]:
        self.agent_summary_limits.append(limit)
        return super().list_agent_task_workflow_summaries_for_collection(
            limit=limit,
            workflow_filter=workflow_filter,
            recent_since=recent_since,
        )


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


def test_workflow_collection_does_not_advertise_next_offset_past_bounded_window() -> None:
    now = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    workflows = [
        _workflow_record(
            f"wf-{index:04d}",
            WorkflowState.ASSIGNED,
            now - timedelta(seconds=index),
        )
        for index in range(WORKFLOW_LIST_MAX_OFFSET + 2)
    ]

    collection = build_workflow_collection(
        workflows,
        limit=1,
        offset=WORKFLOW_LIST_MAX_OFFSET,
        workflow_filter=WorkflowListFilter.ALL,
        now=now,
    )

    assert collection.pagination is not None
    assert collection.pagination.truncated is True
    assert collection.pagination.has_next is False
    assert collection.pagination.next_offset is None


def test_build_workflows_merges_correlated_event_records_into_full_timeline() -> None:
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    correlation_id = "orch-shared-event-workflow"

    workflows = build_workflows(
        [],
        [
            EventRecord(
                event_id="event-2",
                github_event=GitHubEventType.PULL_REQUEST,
                correlation_id=correlation_id,
                repo_full_name="riseos/example",
                pr_number=17,
                pr_merged=True,
                received_at=base + timedelta(minutes=1),
                raw_action="closed",
            ),
            EventRecord(
                event_id="event-1",
                github_event=GitHubEventType.PUSH,
                correlation_id=correlation_id,
                repo_full_name="riseos/example",
                branch="agent-integration",
                commit_sha="abc123",
                received_at=base,
            ),
        ],
    )

    assert len(workflows) == 1
    workflow = workflows[0]
    assert workflow.workflow_id == f"wf-{correlation_id}"
    assert workflow.current_state == WorkflowState.MERGED
    assert workflow.last_activity_at == base + timedelta(minutes=1)
    assert [event.canonical_state for event in workflow.timeline] == [
        WorkflowState.CIRCUIT_WORKING,
        WorkflowState.MERGED,
    ]
    assert workflow.timeline[1].previous_state == workflow.timeline[0].state
    assert workflow.route_history == ["Circuit: CIRCUIT_WORKING", "Human: MERGED"]


def test_build_workflows_keeps_distinct_ref_review_and_event_workflows() -> None:
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)

    workflows = build_workflows(
        [_review_item("review-push", base)],
        [
            EventRecord(
                event_id="duplicate-push-event",
                github_event=GitHubEventType.PUSH,
                repo_full_name="riseos/example",
                branch="agent-review-push",
                commit_sha="sha-review-push",
                received_at=base + timedelta(minutes=2),
            ),
            EventRecord(
                event_id="unrelated-push-event",
                github_event=GitHubEventType.PUSH,
                repo_full_name="riseos/example",
                branch="agent-unrelated",
                commit_sha="unrelated-sha",
                received_at=base + timedelta(minutes=1),
            ),
        ],
    )

    assert [workflow.workflow_id for workflow in workflows] == [
        "wf-unrelated-push-event",
        "wf-review-push",
    ]


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


def test_workflow_endpoint_storage_path_does_not_merge_global_agent_tasks(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    client = client_with_secret()
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)

    storage.save_review_work_item(_review_item("persisted-review", base))
    agent_task_store.save_agent_task(
        _agent_task("global-memory-agent", AgentTaskStatus.QUEUED, base + timedelta(minutes=1))
    )
    app.state.storage = storage

    response = client.get("/api/v1/workflows?filter=all&limit=10")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == ["wf-persisted-review"]
    assert body["pagination"]["returned"] == 1
    assert body["pagination"]["total"] == 1
    assert body["pagination"]["unfiltered_total"] == 1


def test_workflow_endpoint_storage_path_ignores_in_memory_agent_task_store(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    persisted_task_store = SQLiteAgentTaskStore(db_path)
    client = client_with_secret()
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)

    storage.save_review_work_item(_review_item("persisted-review", base))
    persisted_task_store.save_agent_task(
        _agent_task("persisted-agent", AgentTaskStatus.QUEUED, base + timedelta(minutes=2))
    )
    agent_task_store.save_agent_task(
        _agent_task("in-memory-agent", AgentTaskStatus.QUEUED, base + timedelta(minutes=1))
    )
    app.state.storage = storage
    app.state.agent_task_store = agent_task_store

    response = client.get("/api/v1/workflows?filter=all&limit=10")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-agent-task-persisted-agent",
        "wf-persisted-review",
    ]
    assert body["pagination"]["returned"] == 2
    assert body["pagination"]["total"] == 2
    assert body["pagination"]["unfiltered_total"] == 2


def test_workflow_endpoint_uses_summary_records_without_detail_payloads(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = SummaryOnlyWorkflowListSQLiteStateStore(db_path)
    task_store = SummaryOnlyWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    now = datetime.now(UTC)
    review_detail_sentinel = "runtime-context-sentinel-" + ("x" * 10_000)
    task_detail_sentinel = "agent-task-detail-sentinel-" + ("x" * 10_000)
    review_item = _review_item("compact-review", now)
    review_item.runtime_validation_context = {"large_payload": review_detail_sentinel}
    review_item.agent_bus_dispatch_error = review_detail_sentinel
    storage.save_review_work_item(review_item)
    task = _agent_task("compact-agent", AgentTaskStatus.QUEUED, now + timedelta(minutes=1))
    task.body = task_detail_sentinel
    task.instructions = [task_detail_sentinel]
    task.execution_evidence = {"large_payload": task_detail_sentinel}
    task.lifecycle_events[-1].metadata = {"large_payload": task_detail_sentinel}
    task_store.save_agent_task(task)
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=all&limit=10")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-agent-task-compact-agent",
        "wf-compact-review",
    ]
    assert all("timeline" not in workflow for workflow in body["workflows"])
    assert all("route_history" not in workflow for workflow in body["workflows"])
    assert review_detail_sentinel not in response.text
    assert task_detail_sentinel not in response.text


def test_workflow_endpoint_bounded_sqlite_pages_across_sources_by_activity(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    storage.save_event_record(
        EventRecord(
            event_id="newest-event",
            github_event=GitHubEventType.PUSH,
            repo_full_name="riseos/example",
            branch="agent-newest",
            commit_sha="sha-newest",
            received_at=base + timedelta(minutes=3),
        )
    )
    storage.save_review_work_item(_review_item("middle-review", base + timedelta(minutes=2)))
    task_store.save_agent_task(
        _agent_task("oldest-agent", AgentTaskStatus.QUEUED, base + timedelta(minutes=1))
    )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=all&limit=2&offset=1")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-middle-review",
        "wf-agent-task-oldest-agent",
    ]
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


def test_workflow_endpoint_bounded_sqlite_fetches_only_candidate_window_per_source(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = QueryShapeWorkflowListSQLiteStateStore(db_path)
    task_store = QueryShapeWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)

    for index in range(20):
        storage.save_event_record(
            EventRecord(
                event_id=f"query-event-{index}",
                github_event=GitHubEventType.PUSH,
                repo_full_name="riseos/example",
                branch=f"query-event-{index}",
                commit_sha=f"event-sha-{index}",
                received_at=base + timedelta(minutes=index),
            )
        )
        storage.save_review_work_item(_review_item(f"query-review-{index}", base + timedelta(minutes=index)))
        task_store.save_agent_task(
            _agent_task(f"query-agent-{index}", AgentTaskStatus.QUEUED, base + timedelta(minutes=index))
        )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=all&limit=3&offset=4")

    assert response.status_code == 200
    body = response.json()
    assert len(body["workflows"]) == 3
    assert body["pagination"]["total"] == 60
    assert body["pagination"]["unfiltered_total"] == 60
    assert storage.review_summary_limits == [7]
    assert storage.event_summary_limits == [7]
    assert task_store.agent_summary_limits == [7]


def test_workflow_endpoint_bounded_sqlite_page_can_be_filled_by_one_source(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)

    for index in range(12):
        task_store.save_agent_task(
            _agent_task(
                f"dominant-agent-{index:02d}",
                AgentTaskStatus.QUEUED,
                base + timedelta(minutes=100 - index),
            )
        )
    for index in range(4):
        storage.save_review_work_item(_review_item(f"older-review-{index}", base + timedelta(minutes=index)))
        storage.save_event_record(
            EventRecord(
                event_id=f"older-event-{index}",
                github_event=GitHubEventType.PUSH,
                repo_full_name="riseos/example",
                branch=f"older-event-{index}",
                commit_sha=f"older-sha-{index}",
                received_at=base + timedelta(minutes=index),
            )
        )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=all&limit=4&offset=5")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-agent-task-dominant-agent-05",
        "wf-agent-task-dominant-agent-06",
        "wf-agent-task-dominant-agent-07",
        "wf-agent-task-dominant-agent-08",
    ]
    assert all("timeline" not in workflow for workflow in body["workflows"])
    assert all("route_history" not in workflow for workflow in body["workflows"])
    assert body["pagination"]["returned"] == 4
    assert body["pagination"]["total"] == 20
    assert body["pagination"]["unfiltered_total"] == 20
    assert body["pagination"]["has_next"] is True
    assert body["pagination"]["next_offset"] == 9


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


def test_workflow_endpoint_bounded_sqlite_agent_task_recent_filter_uses_status_timestamps(
    tmp_path,
) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    now = datetime.now(UTC)
    stale = now - timedelta(days=30)
    recent = now - timedelta(minutes=5)
    recent_completed = _agent_task("recent-completed", AgentTaskStatus.COMPLETED, stale)
    recent_completed.updated_at = stale
    recent_completed.completed_at = recent
    stale_completed = _agent_task("stale-completed", AgentTaskStatus.COMPLETED, stale)
    stale_completed.updated_at = stale
    stale_completed.completed_at = stale
    task_store.save_agent_task(stale_completed)
    task_store.save_agent_task(recent_completed)
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=recent&limit=10&recent_days=14")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-agent-task-recent-completed",
    ]
    assert datetime.fromisoformat(
        body["workflows"][0]["last_activity_at"].replace("Z", "+00:00")
    ) == recent
    assert body["pagination"]["returned"] == 1
    assert body["pagination"]["total"] == 1
    assert body["pagination"]["unfiltered_total"] == 2
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


def test_workflow_endpoint_bounded_sqlite_event_totals_are_deduplicated_by_workflow_id(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)

    for index in range(8):
        storage.save_event_record(
            EventRecord(
                event_id=f"shared-event-{index}",
                github_event=GitHubEventType.PUSH,
                correlation_id="orch-shared-event-workflow",
                repo_full_name="riseos/example",
                branch="agent-shared",
                commit_sha=f"shared-sha-{index}",
                received_at=base + timedelta(minutes=index),
            )
        )
    for index in range(2):
        storage.save_event_record(
            EventRecord(
                event_id=f"unique-event-{index}",
                github_event=GitHubEventType.PUSH,
                repo_full_name="riseos/example",
                branch=f"agent-unique-{index}",
                commit_sha=f"unique-sha-{index}",
                received_at=base + timedelta(hours=1, minutes=index),
            )
        )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=all&limit=2")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-unique-event-1",
        "wf-unique-event-0",
    ]
    assert body["pagination"]["returned"] == 2
    assert body["pagination"]["total"] == 3
    assert body["pagination"]["unfiltered_total"] == 3
    assert body["pagination"]["has_next"] is True
    assert body["pagination"]["next_offset"] == 2


def test_workflow_endpoint_bounded_sqlite_fills_page_after_event_workflow_deduplication(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)

    for index in range(10):
        storage.save_event_record(
            EventRecord(
                event_id=f"shared-event-{index}",
                github_event=GitHubEventType.PUSH,
                correlation_id="orch-shared-page",
                repo_full_name="riseos/example",
                branch="agent-shared",
                commit_sha=f"shared-sha-{index}",
                received_at=base + timedelta(hours=1, minutes=index),
            )
        )
    for index in range(4):
        storage.save_event_record(
            EventRecord(
                event_id=f"unique-page-event-{index}",
                github_event=GitHubEventType.PUSH,
                repo_full_name="riseos/example",
                branch=f"agent-unique-page-{index}",
                commit_sha=f"unique-page-sha-{index}",
                received_at=base + timedelta(minutes=index),
            )
        )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=all&limit=3")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-orch-shared-page",
        "wf-unique-page-event-3",
        "wf-unique-page-event-2",
    ]
    assert body["pagination"]["returned"] == 3
    assert body["pagination"]["total"] == 5
    assert body["pagination"]["unfiltered_total"] == 5
    assert body["pagination"]["has_next"] is True
    assert body["pagination"]["next_offset"] == 3


def test_workflow_endpoint_bounded_sqlite_event_summary_keeps_created_at_without_timeline(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)

    storage.save_event_record(
        EventRecord(
            event_id="event-created",
            github_event=GitHubEventType.PUSH,
            correlation_id="orch-summary-created-at",
            repo_full_name="riseos/example",
            branch="agent-summary",
            commit_sha="summary-sha",
            received_at=base,
        )
    )
    storage.save_event_record(
        EventRecord(
            event_id="event-updated",
            github_event=GitHubEventType.PULL_REQUEST_REVIEW,
            correlation_id="orch-summary-created-at",
            repo_full_name="riseos/example",
            pr_number=17,
            received_at=base + timedelta(minutes=2),
            raw_action="submitted",
        )
    )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=all&limit=10")

    assert response.status_code == 200
    workflow = response.json()["workflows"][0]
    assert workflow["workflow_id"] == "wf-orch-summary-created-at"
    assert workflow["current_state"] == "BB2_REVIEWING"
    assert datetime.fromisoformat(workflow["created_at"].replace("Z", "+00:00")) == base
    assert datetime.fromisoformat(workflow["updated_at"].replace("Z", "+00:00")) == base + timedelta(minutes=2)
    assert datetime.fromisoformat(workflow["last_activity_at"].replace("Z", "+00:00")) == base + timedelta(minutes=2)
    assert "timeline" not in workflow
    assert "route_history" not in workflow

    detail = client.get("/api/v1/workflows/wf-orch-summary-created-at")
    assert detail.status_code == 200
    assert [event["source"] for event in detail.json()["timeline"]] == [
        "github_webhook",
        "github_webhook",
    ]


def test_workflow_endpoint_bounded_sqlite_event_duplicates_review_item_identity(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    storage.save_review_work_item(
        ReviewWorkItem(
            id="review-pr-17",
            created_at=base,
            updated_at=base,
            repo_full_name="riseos/example",
            event_type=GitHubEventType.PULL_REQUEST,
            branch="feature/review-pr-17",
            commit_sha="review-sha",
            pr_number=17,
        )
    )
    storage.save_event_record(
        EventRecord(
            event_id="duplicate-pr-event",
            github_event=GitHubEventType.PULL_REQUEST,
            correlation_id="orch-duplicate-pr-event",
            repo_full_name="riseos/example",
            pr_number=17,
            received_at=base + timedelta(minutes=1),
            raw_action="opened",
        )
    )
    storage.save_event_record(
        EventRecord(
            event_id="duplicate-pr-push",
            github_event=GitHubEventType.PUSH,
            correlation_id="orch-duplicate-pr-event",
            repo_full_name="riseos/example",
            branch="feature/review-pr-17",
            commit_sha="review-sha",
            received_at=base + timedelta(minutes=2),
        )
    )
    storage.save_event_record(
        EventRecord(
            event_id="unique-event",
            github_event=GitHubEventType.PUSH,
            repo_full_name="riseos/example",
            branch="agent-unique",
            commit_sha="unique-sha",
            received_at=base + timedelta(minutes=3),
        )
    )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=all&limit=10")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-unique-event",
        "wf-review-pr-17",
    ]
    assert body["pagination"]["returned"] == 2
    assert body["pagination"]["total"] == 2
    assert body["pagination"]["unfiltered_total"] == 2
    assert body["pagination"]["has_next"] is False


def test_workflow_endpoint_bounded_sqlite_keeps_distinct_ref_workflows(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    storage.save_review_work_item(_review_item("review-push", base))
    storage.save_event_record(
        EventRecord(
            event_id="duplicate-push-event",
            github_event=GitHubEventType.PUSH,
            repo_full_name="riseos/example",
            branch="agent-review-push",
            commit_sha="sha-review-push",
            received_at=base + timedelta(minutes=1),
        )
    )
    storage.save_event_record(
        EventRecord(
            event_id="unrelated-push-event",
            github_event=GitHubEventType.PUSH,
            repo_full_name="riseos/example",
            branch="agent-unrelated",
            commit_sha="unrelated-sha",
            received_at=base + timedelta(minutes=2),
        )
    )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=all&limit=10")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-unrelated-push-event",
        "wf-review-push",
    ]
    assert body["pagination"]["total"] == 2
    assert body["pagination"]["unfiltered_total"] == 2


def test_workflow_endpoint_bounded_sqlite_keeps_distinct_fallback_workflows(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    storage.save_review_work_item(
        ReviewWorkItem(
            id="generic-review",
            created_at=base,
            updated_at=base,
            repo_full_name="riseos/example",
            event_type=GitHubEventType.PUSH,
        )
    )
    storage.save_event_record(
        EventRecord(
            event_id="generic-event",
            github_event=GitHubEventType.PUSH,
            repo_full_name="riseos/example",
            received_at=base + timedelta(minutes=1),
        )
    )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=all&limit=10")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == [
        "wf-generic-event",
        "wf-generic-review",
    ]
    assert body["pagination"]["returned"] == 2
    assert body["pagination"]["total"] == 2
    assert body["pagination"]["unfiltered_total"] == 2
    assert body["pagination"]["has_next"] is False


def test_workflow_endpoint_bounded_sqlite_deduplicates_matching_fallback_workflows(tmp_path) -> None:
    db_path = str(tmp_path / "orchestrator.db")
    storage = NoFullWorkflowListSQLiteStateStore(db_path)
    task_store = NoFullWorkflowListSQLiteAgentTaskStore(db_path)
    client = client_with_secret(db_path=db_path)
    base = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    storage.save_review_work_item(
        ReviewWorkItem(
            id="shared-fallback",
            created_at=base,
            updated_at=base,
            repo_full_name="riseos/example",
            event_type=GitHubEventType.PUSH,
        )
    )
    storage.save_event_record(
        EventRecord(
            event_id="shared-fallback-event",
            github_event=GitHubEventType.PUSH,
            correlation_id="shared-fallback",
            repo_full_name="riseos/example",
            received_at=base + timedelta(minutes=1),
        )
    )
    app.state.storage = storage
    app.state.agent_task_store = task_store

    response = client.get("/api/v1/workflows?filter=all&limit=10")

    assert response.status_code == 200
    body = response.json()
    assert [workflow["workflow_id"] for workflow in body["workflows"]] == ["wf-shared-fallback"]
    assert body["pagination"]["returned"] == 1
    assert body["pagination"]["total"] == 1
    assert body["pagination"]["unfiltered_total"] == 1
    assert body["pagination"]["has_next"] is False


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
    storage.save_event_record(
        EventRecord(
            event_id="target-review-event",
            github_event=GitHubEventType.PULL_REQUEST_REVIEW,
            correlation_id=target.correlation_id,
            repo_full_name="riseos/example",
            pr_number=17,
            received_at=base + timedelta(minutes=2),
            raw_action="submitted",
        )
    )
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
    assert detail_body["current_state"] == "BB2_REVIEWING"
    assert len(detail_body["timeline"]) == 2
    assert detail_body["timeline"][0]["source"] == "github_webhook"
    assert detail_body["timeline"][0]["commit_sha"] == "target-sha"
    assert detail_body["timeline"][1]["new_state"] == "BB2_REVIEWING"
    assert detail_body["timeline"][1]["previous_state"] == "CIRCUIT_IN_PROGRESS"
    assert timeline.status_code == 200
    assert len(timeline.json()["events"]) == 2
    assert timeline.json()["events"][0]["commit_sha"] == "target-sha"
    assert timeline.json()["events"][1]["new_state"] == "BB2_REVIEWING"


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


def test_workflow_endpoint_rejects_unbounded_offset() -> None:
    client = client_with_secret()

    response = client.get(f"/api/v1/workflows?offset={WORKFLOW_LIST_MAX_OFFSET + 1}")

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
