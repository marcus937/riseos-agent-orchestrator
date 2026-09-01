from __future__ import annotations

import json
import logging
import os
import statistics
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent_tasks import AgentTask, AgentTaskWorkflowSummary, SQLiteAgentTaskStore, agent_task_store
from app.config import get_settings
from app.event_store import EventRecord, EventWorkflowSummary, event_store
from app.main import app
from app.operational_logging import LOGGER_NAME
from app.orchestrator_snapshot import ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT, ORCHESTRATOR_SNAPSHOT_EVENT_LIMIT
from app.review_queue import ReviewLifecycleVisibility, ReviewWorkItem, ReviewWorkItemWorkflowSummary, review_queue
from app.storage import SQLiteStateStore
from tests.mission_control_polling_fixture import (
    PRODUCTION_DETAIL_SENTINEL,
    PRODUCTION_SECRET_SENTINEL,
    MissionControlProductionFixture,
    seed_mission_control_production_fixture,
)


DEFAULT_SNAPSHOT_SERIALIZED_BYTES_BUDGET = 280_000
DEFAULT_WORKFLOW_PAGE_SERIALIZED_BYTES_BUDGET = 60_000
TWO_ITEM_WORKFLOW_PAGE_SERIALIZED_BYTES_BUDGET = 3_500
BENCHMARK_ENV_VAR = "RUN_MISSION_CONTROL_POLLING_BENCHMARK"
BENCHMARK_ITERATIONS_ENV_VAR = "MISSION_CONTROL_POLLING_BENCHMARK_ITERATIONS"
BENCHMARK_OUTPUT_ENV_VAR = "MISSION_CONTROL_POLLING_BENCHMARK_OUTPUT"

MISSION_CONTROL_POLLING_REQUESTS = (
    ("snapshot", "/api/v1/orchestrator/snapshot"),
    ("workflow_default_page", "/api/v1/workflows"),
    ("workflow_two_item_page", "/api/v1/workflows?limit=2"),
)


class InstrumentedMissionControlSQLiteStateStore(SQLiteStateStore):
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path, max_review_items=1_000)
        self.snapshot_record_calls: list[dict[str, object]] = []
        self.snapshot_record_hydration_count = 0
        self.lifecycle_limits: list[int | None] = []
        self.lifecycle_hydration_count = 0
        self.recent_event_limits: list[int | None] = []
        self.recent_event_hydration_count = 0
        self.snapshot_count_calls: list[str | None] = []
        self.event_count_calls = 0
        self.review_workflow_count_calls: list[dict[str, object]] = []
        self.event_workflow_count_calls: list[dict[str, object]] = []
        self.event_snapshot_category_count_calls: list[str] = []
        self.workflow_summary_count_calls = 0
        self.review_queue_stats_calls = 0
        self.worker_stats_calls = 0
        self.recent_failure_limits: list[int] = []
        self.review_summary_limits: list[int] = []
        self.review_summary_hydration_count = 0
        self.event_summary_limits: list[int] = []
        self.event_summary_hydration_count = 0

    def list_review_work_items(self) -> list[ReviewWorkItem]:
        raise AssertionError("Mission Control polling must not hydrate all review work items")

    def list_event_records_for_workflow_collection(self, *args: Any, **kwargs: Any) -> list[EventRecord]:
        raise AssertionError("Mission Control workflow polling must use event summaries, not full event records")

    def _review_work_item_from_row(self, row: Any) -> ReviewWorkItem:
        raise AssertionError("Mission Control polling must not deserialize full review work item rows")

    def recent_events(self, limit: int = 50) -> list[EventRecord]:
        self.recent_event_limits.append(limit)
        records = super().recent_events(limit=limit)
        self.recent_event_hydration_count += len(records)
        return records

    def list_review_work_item_snapshot_records(
        self,
        *,
        limit: int | None = None,
        collection: str | None = None,
    ) -> list[ReviewWorkItem]:
        self.snapshot_record_calls.append({"limit": limit, "collection": collection})
        records = super().list_review_work_item_snapshot_records(limit=limit, collection=collection)
        self.snapshot_record_hydration_count += len(records)
        return records

    def list_lifecycle_visibility_records(
        self,
        *,
        limit: int | None = None,
    ) -> list[ReviewLifecycleVisibility]:
        self.lifecycle_limits.append(limit)
        records = super().list_lifecycle_visibility_records(limit=limit)
        self.lifecycle_hydration_count += len(records)
        return records

    def count_review_work_item_snapshot_records(self, *, collection: str | None = None) -> int:
        self.snapshot_count_calls.append(collection)
        return super().count_review_work_item_snapshot_records(collection=collection)

    def event_count(self) -> int:
        self.event_count_calls += 1
        return super().event_count()

    def count_review_work_items_for_workflow_collection(
        self,
        *,
        workflow_filter: str = "active_recent",
        recent_since: Any = None,
    ) -> int:
        self.review_workflow_count_calls.append(
            {
                "workflow_filter": workflow_filter,
                "recent_since": recent_since is not None,
            }
        )
        return super().count_review_work_items_for_workflow_collection(
            workflow_filter=workflow_filter,
            recent_since=recent_since,
        )

    def count_event_records_for_workflow_collection(
        self,
        *,
        workflow_filter: str = "active_recent",
        recent_since: Any = None,
    ) -> int:
        self.event_workflow_count_calls.append(
            {
                "workflow_filter": workflow_filter,
                "recent_since": recent_since is not None,
            }
        )
        return super().count_event_records_for_workflow_collection(
            workflow_filter=workflow_filter,
            recent_since=recent_since,
        )

    def workflow_summary_counts_for_snapshot(self) -> Any:
        self.workflow_summary_count_calls += 1
        return super().workflow_summary_counts_for_snapshot()

    def _count_event_workflows_for_snapshot_category(self, category: str) -> int:
        self.event_snapshot_category_count_calls.append(category)
        return super()._count_event_workflows_for_snapshot_category(category)

    def review_queue_stats(self) -> Any:
        self.review_queue_stats_calls += 1
        return super().review_queue_stats()

    def worker_stats(self, *, auto_processing_enabled: bool) -> Any:
        self.worker_stats_calls += 1
        return super().worker_stats(auto_processing_enabled=auto_processing_enabled)

    def list_recent_failures(self, *, limit: int = 20) -> Any:
        self.recent_failure_limits.append(limit)
        return super().list_recent_failures(limit=limit)

    def list_review_work_item_summary_records_for_workflow_collection(
        self,
        *,
        limit: int,
        workflow_filter: str = "active_recent",
        recent_since: Any = None,
    ) -> list[ReviewWorkItemWorkflowSummary]:
        self.review_summary_limits.append(limit)
        records = super().list_review_work_item_summary_records_for_workflow_collection(
            limit=limit,
            workflow_filter=workflow_filter,
            recent_since=recent_since,
        )
        self.review_summary_hydration_count += len(records)
        assert all(isinstance(record, ReviewWorkItemWorkflowSummary) for record in records)
        return records

    def list_event_workflow_summary_records_for_collection(
        self,
        *,
        limit: int,
        workflow_filter: str = "active_recent",
        recent_since: Any = None,
    ) -> list[EventWorkflowSummary]:
        self.event_summary_limits.append(limit)
        records = super().list_event_workflow_summary_records_for_collection(
            limit=limit,
            workflow_filter=workflow_filter,
            recent_since=recent_since,
        )
        self.event_summary_hydration_count += len(records)
        assert all(isinstance(record, EventWorkflowSummary) for record in records)
        return records


class InstrumentedMissionControlSQLiteAgentTaskStore(SQLiteAgentTaskStore):
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self.agent_summary_limits: list[int] = []
        self.agent_summary_hydration_count = 0
        self.agent_workflow_count_calls: list[dict[str, object]] = []
        self.workflow_summary_count_calls = 0

    def list_agent_tasks(self) -> list[AgentTask]:
        raise AssertionError("Mission Control polling must not hydrate all agent tasks")

    def list_agent_tasks_for_workflow_collection(self, *args: Any, **kwargs: Any) -> list[AgentTask]:
        raise AssertionError("Mission Control workflow polling must use agent task summaries")

    def list_agent_task_workflow_summaries_for_collection(
        self,
        *,
        limit: int,
        workflow_filter: str = "active_recent",
        recent_since: Any = None,
    ) -> list[AgentTaskWorkflowSummary]:
        self.agent_summary_limits.append(limit)
        records = super().list_agent_task_workflow_summaries_for_collection(
            limit=limit,
            workflow_filter=workflow_filter,
            recent_since=recent_since,
        )
        self.agent_summary_hydration_count += len(records)
        assert all(isinstance(record, AgentTaskWorkflowSummary) for record in records)
        return records

    def count_agent_tasks_for_workflow_collection(
        self,
        *,
        workflow_filter: str = "active_recent",
        recent_since: Any = None,
    ) -> int:
        self.agent_workflow_count_calls.append(
            {
                "workflow_filter": workflow_filter,
                "recent_since": recent_since is not None,
            }
        )
        return super().count_agent_tasks_for_workflow_collection(
            workflow_filter=workflow_filter,
            recent_since=recent_since,
        )

    def workflow_summary_counts_for_snapshot(self) -> Any:
        self.workflow_summary_count_calls += 1
        return super().workflow_summary_counts_for_snapshot()


def test_mission_control_production_snapshot_has_bounded_queries_and_size_budget(tmp_path: Path) -> None:
    client, storage, _task_store, fixture = _seeded_client(tmp_path)

    response = client.get("/api/v1/orchestrator/snapshot")

    assert response.status_code == 200
    body = response.json()
    workforce = body["workforce"]
    assert workforce["meta"]["agents"] == {
        "returned": ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT,
        "total": fixture.agent_lifecycle_record_count,
        "limit": ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT,
        "truncated": True,
    }
    assert workforce["meta"]["issues"]["total"] == fixture.issue_record_count
    assert workforce["meta"]["prs"]["total"] == fixture.pr_record_count
    assert workforce["meta"]["events"] == {
        "returned": ORCHESTRATOR_SNAPSHOT_EVENT_LIMIT,
        "total": fixture.event_record_count,
        "limit": ORCHESTRATOR_SNAPSHOT_EVENT_LIMIT,
        "truncated": True,
    }
    assert body["workflows"]["active"] == fixture.total_workflow_count
    assert storage.snapshot_record_calls == [
        {"limit": ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT, "collection": None},
        {"limit": ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT, "collection": "issues"},
        {"limit": ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT, "collection": "prs"},
    ]
    assert storage.lifecycle_limits == [ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT]
    assert storage.recent_event_limits == [ORCHESTRATOR_SNAPSHOT_EVENT_LIMIT]
    assert storage.snapshot_count_calls == [None, "issues", "prs"]
    assert storage.event_count_calls == 2
    assert storage.workflow_summary_count_calls == 1
    assert storage.review_workflow_count_calls == []
    assert storage.event_workflow_count_calls == [
        {"workflow_filter": "active", "recent_since": False},
    ]
    assert storage.event_snapshot_category_count_calls == ["blocked", "reviewing"]
    assert _task_store.workflow_summary_count_calls == 1
    assert storage.review_queue_stats_calls == 1
    assert storage.worker_stats_calls == 1
    assert storage.recent_failure_limits == [20]
    assert storage.snapshot_record_hydration_count <= ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT * 3
    assert storage.lifecycle_hydration_count <= ORCHESTRATOR_SNAPSHOT_COLLECTION_LIMIT
    assert storage.recent_event_hydration_count <= ORCHESTRATOR_SNAPSHOT_EVENT_LIMIT
    assert _serialized_response_bytes(response) <= DEFAULT_SNAPSHOT_SERIALIZED_BYTES_BUDGET
    assert PRODUCTION_DETAIL_SENTINEL not in response.text
    assert PRODUCTION_SECRET_SENTINEL not in response.text


def test_mission_control_default_workflow_page_has_bounded_queries_and_size_budget(tmp_path: Path) -> None:
    client, storage, task_store, fixture = _seeded_client(tmp_path)

    response = client.get("/api/v1/workflows")

    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["returned"] == 50
    assert body["pagination"]["total"] == fixture.total_workflow_count
    assert body["pagination"]["unfiltered_total"] == fixture.total_workflow_count
    expected_count_calls = [
        {"workflow_filter": "active_recent", "recent_since": True},
        {"workflow_filter": "all", "recent_since": False},
    ]
    assert storage.review_workflow_count_calls == expected_count_calls
    assert storage.event_workflow_count_calls == expected_count_calls
    assert task_store.agent_workflow_count_calls == expected_count_calls
    assert storage.review_summary_limits == [50]
    assert storage.event_summary_limits == [50]
    assert task_store.agent_summary_limits == [50]
    assert storage.review_summary_hydration_count <= 50
    assert storage.event_summary_hydration_count <= 50
    assert task_store.agent_summary_hydration_count <= 50
    assert all("timeline" not in workflow for workflow in body["workflows"])
    assert all("route_history" not in workflow for workflow in body["workflows"])
    assert _serialized_response_bytes(response) <= DEFAULT_WORKFLOW_PAGE_SERIALIZED_BYTES_BUDGET
    assert PRODUCTION_DETAIL_SENTINEL not in response.text
    assert PRODUCTION_SECRET_SENTINEL not in response.text


def test_mission_control_two_item_workflow_page_does_not_hydrate_complete_history(tmp_path: Path) -> None:
    client, storage, task_store, fixture = _seeded_client(tmp_path)

    response = client.get("/api/v1/workflows?limit=2")

    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["returned"] == 2
    assert body["pagination"]["total"] == fixture.total_workflow_count
    assert body["pagination"]["unfiltered_total"] == fixture.total_workflow_count
    expected_count_calls = [
        {"workflow_filter": "active_recent", "recent_since": True},
        {"workflow_filter": "all", "recent_since": False},
    ]
    assert storage.review_workflow_count_calls == expected_count_calls
    assert storage.event_workflow_count_calls == expected_count_calls
    assert task_store.agent_workflow_count_calls == expected_count_calls
    assert storage.review_summary_limits == [2]
    assert storage.event_summary_limits == [2]
    assert task_store.agent_summary_limits == [2]
    assert storage.review_summary_hydration_count <= 2
    assert storage.event_summary_hydration_count <= 2
    assert task_store.agent_summary_hydration_count <= 2
    assert _serialized_response_bytes(response) <= TWO_ITEM_WORKFLOW_PAGE_SERIALIZED_BYTES_BUDGET
    assert PRODUCTION_DETAIL_SENTINEL not in response.text
    assert PRODUCTION_SECRET_SENTINEL not in response.text


def test_mission_control_polling_observability_is_scalar_only(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _storage, _task_store, fixture = _seeded_client(tmp_path)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    caplog.clear()

    snapshot = client.get("/api/v1/orchestrator/snapshot")
    workflows = client.get("/api/v1/workflows?limit=2")

    assert snapshot.status_code == 200
    assert workflows.status_code == 200
    logs = _polling_logs(caplog)
    assert {record["endpoint"] for record in logs} == {
        "/api/v1/orchestrator/snapshot",
        "/api/v1/workflows",
    }
    snapshot_log = next(record for record in logs if record["endpoint"] == "/api/v1/orchestrator/snapshot")
    workflow_log = next(record for record in logs if record["endpoint"] == "/api/v1/workflows")
    assert snapshot_log["returned_count"] == 175
    assert snapshot_log["total_count"] == (
        fixture.agent_lifecycle_record_count
        + fixture.issue_record_count
        + fixture.pr_record_count
        + fixture.event_record_count
    )
    assert snapshot_log["serialized_bytes"] == _model_serialized_bytes(snapshot.json())
    assert workflow_log["returned_count"] == 2
    assert workflow_log["total_count"] == fixture.total_workflow_count
    assert workflow_log["serialized_bytes"] == _model_serialized_bytes(workflows.json())
    assert workflow_log["limit"] == 2
    assert workflow_log["offset"] == 0
    assert workflow_log["filter"] == "active_recent"
    assert workflow_log["recent_days"] == 14
    for record in logs:
        assert record["duration_ms"] >= 0
        assert record["serialized_bytes"] > 0
        serialized_log = json.dumps(record, sort_keys=True)
        assert PRODUCTION_DETAIL_SENTINEL not in serialized_log
        assert PRODUCTION_SECRET_SENTINEL not in serialized_log


def test_mission_control_production_fixture_diagnostics_do_not_log_payloads(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    caplog.clear()

    storage = SQLiteStateStore(str(tmp_path / "orchestrator.db"), max_review_items=1_000)
    task_store = SQLiteAgentTaskStore(str(tmp_path / "orchestrator.db"))
    seed_mission_control_production_fixture(storage, task_store)

    serialized_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert PRODUCTION_DETAIL_SENTINEL not in serialized_logs
    assert PRODUCTION_SECRET_SENTINEL not in serialized_logs

    storage_payload_logs = [
        payload
        for payload in _json_log_payloads(caplog)
        if str(payload.get("event", "")).startswith("wf_chain_metadata_storage_")
    ]
    assert storage_payload_logs
    assert any(payload.get("raw_json_stored_present") for payload in storage_payload_logs)
    assert any(payload.get("raw_json_stored_bytes", 0) > 0 for payload in storage_payload_logs)
    for payload in storage_payload_logs:
        assert "raw_json_stored" not in payload
        assert "raw_json_loaded" not in payload


@pytest.mark.skipif(
    os.getenv(BENCHMARK_ENV_VAR) != "1",
    reason=f"Set {BENCHMARK_ENV_VAR}=1 to record Mission Control polling latency.",
)
def test_mission_control_polling_integration_benchmark_records_wall_clock_latency(
    tmp_path: Path,
) -> None:
    iterations = int(os.getenv(BENCHMARK_ITERATIONS_ENV_VAR, "5"))
    client, _storage, _task_store, _fixture = _seeded_client(tmp_path)
    results = {
        "iterations": iterations,
        "requests": [
            _benchmark_request(client, name, path, iterations=iterations)
            for name, path in MISSION_CONTROL_POLLING_REQUESTS
        ],
    }
    output_path = os.getenv(BENCHMARK_OUTPUT_ENV_VAR)
    if output_path:
        Path(output_path).write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    print(json.dumps(results, sort_keys=True))

    assert all(result["status_code"] == 200 for result in results["requests"])


def _seeded_client(
    tmp_path: Path,
) -> tuple[
    TestClient,
    InstrumentedMissionControlSQLiteStateStore,
    InstrumentedMissionControlSQLiteAgentTaskStore,
    MissionControlProductionFixture,
]:
    db_path = str(tmp_path / "orchestrator.db")
    storage = InstrumentedMissionControlSQLiteStateStore(db_path)
    task_store = InstrumentedMissionControlSQLiteAgentTaskStore(db_path)
    fixture = seed_mission_control_production_fixture(storage, task_store)
    get_settings.cache_clear()
    event_store.reset()
    review_queue.reset()
    agent_task_store.reset()
    app.dependency_overrides.clear()
    for state_key in ("storage", "agent_task_store", "workflow_v1_store"):
        if hasattr(app.state, state_key):
            delattr(app.state, state_key)
    settings_cls = get_settings().__class__
    app.dependency_overrides[get_settings] = lambda: settings_cls(
        orchestrator_db_path=db_path,
        orchestrator_admin_token="admin-token",
        require_admin_token_for_debug_reads=False,
        hermes_m2_token="hermes-m2-secret",
        hermes_dgx_token="hermes-dgx-secret",
    )
    app.state.storage = storage
    app.state.agent_task_store = task_store
    return TestClient(app), storage, task_store, fixture


def _serialized_response_bytes(response: Any) -> int:
    return _model_serialized_bytes(response.json())


def _model_serialized_bytes(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))


def _polling_logs(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        payload
        for payload in _json_log_payloads(caplog)
        if payload.get("event") == "mission_control_polling_response"
    ]


def _json_log_payloads(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    for record in caplog.records:
        try:
            payload = json.loads(record.getMessage())
        except json.JSONDecodeError:
            continue
        logs.append(payload)
    return logs


def _benchmark_request(client: TestClient, name: str, path: str, *, iterations: int) -> dict[str, Any]:
    durations_ms: list[float] = []
    response_body: dict[str, Any] | None = None
    status_code = 0
    serialized_bytes = 0
    for _ in range(max(iterations, 1)):
        started_at = perf_counter()
        response = client.get(path)
        durations_ms.append(round((perf_counter() - started_at) * 1000, 3))
        status_code = response.status_code
        serialized_bytes = len(response.content)
        response_body = response.json()
        assert response.status_code == 200
    returned_count, total_count = _response_counts(name, response_body or {})
    return {
        "name": name,
        "path": path,
        "status_code": status_code,
        "returned_count": returned_count,
        "total_count": total_count,
        "serialized_bytes": serialized_bytes,
        "min_ms": min(durations_ms),
        "median_ms": statistics.median(durations_ms),
        "max_ms": max(durations_ms),
    }


def _response_counts(name: str, body: dict[str, Any]) -> tuple[int, int]:
    if name == "snapshot":
        metadata = body["workforce"]["meta"]
        returned = sum(collection["returned"] for collection in metadata.values())
        total = sum(collection["total"] for collection in metadata.values())
        return returned, total
    pagination = body["pagination"]
    return pagination["returned"], pagination["total"]
