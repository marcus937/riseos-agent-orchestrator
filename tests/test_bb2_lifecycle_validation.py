import asyncio
import hashlib
import hmac
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings, get_settings
from app.github_events import GitHubEventType
from app.review_queue import (
    ReviewLifecycleStage,
    ReviewWorkItem,
    ReviewWorkItemStatus,
    process_review_work_item,
)
from app.review_worker import process_queued_review_item
from app.storage import SQLiteStateStore


@contextmanager
def orchestrator_client(tmp_path, **settings_overrides):
    settings = replace(
        Settings(
            github_webhook_secret="ci-webhook-secret",
            orchestrator_admin_token="ci-admin-token",
        ),
        **settings_overrides,
    )
    main_module.app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(main_module.app) as client:
            store = SQLiteStateStore(str(tmp_path / f"{uuid4()}.db"))
            main_module.app.state.storage = store
            yield client, settings, store
    finally:
        main_module.app.dependency_overrides.clear()
        main_module.app.state.storage = None


def test_queue_creation_persistence_and_diagnostics_endpoints(tmp_path):
    payload = _pull_request_payload()

    with orchestrator_client(tmp_path) as (client, _settings, _store):
        response = client.post(
            "/webhooks/github",
            content=json.dumps(payload).encode("utf-8"),
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": _signature(payload),
            },
        )

        assert response.status_code == 200
        assert response.json()["event_type"] == "pull_request"

        queue = client.get("/debug/review-queue").json()
        assert len(queue) == 1
        assert queue[0]["repo_full_name"] == "marcus937/riseos-agent-orchestrator"
        assert queue[0]["status"] == ReviewWorkItemStatus.PENDING_REVIEW
        assert queue[0]["lifecycle_stage"] == ReviewLifecycleStage.REVIEW_QUEUED

        stats = client.get("/debug/review-queue/stats").json()
        assert stats["counters"]["pending_review_count"] == 1
        workers = client.get("/debug/workers/stats").json()
        assert workers["auto_processing_enabled"] is False
        lifecycle = client.get("/debug/review-lifecycle").json()
        assert lifecycle[0]["lifecycle_stage"] == ReviewLifecycleStage.REVIEW_QUEUED


def test_worker_claim_transition_and_review_completed_success_path(tmp_path):
    async def successful_processor(item, _settings):
        assert item.status == ReviewWorkItemStatus.REVIEWING
        assert item.lifecycle_stage == ReviewLifecycleStage.WORKER_CLAIMED
        main_module.record_lifecycle_stage(item, ReviewLifecycleStage.REVIEW_STARTED)
        response = process_review_work_item(item)
        main_module.record_lifecycle_stage(response.work_item, ReviewLifecycleStage.REVIEW_COMPLETED)
        return response

    with orchestrator_client(tmp_path) as (_client, settings, store):
        item = _review_item()
        store.save_review_work_item(item)

        response = asyncio.run(process_queued_review_item(item.id, settings, store, successful_processor))

        assert response is not None
        saved = store.get_review_work_item(item.id)
        assert saved is not None
        assert saved.status == ReviewWorkItemStatus.APPROVED_FOR_HUMAN_REVIEW
        assert saved.worker_claimed_at is not None
        assert saved.review_started_at is not None
        assert saved.review_completed_at is not None
        assert saved.lifecycle_stage == ReviewLifecycleStage.REVIEW_COMPLETED


def test_review_failed_path_persists_exception_text_and_recent_failure(tmp_path):
    async def failing_processor(_item, _settings):
        raise RuntimeError("mock lifecycle exception")

    with orchestrator_client(tmp_path) as (client, settings, store):
        item = _review_item()
        store.save_review_work_item(item)

        response = asyncio.run(process_queued_review_item(item.id, settings, store, failing_processor))

        assert response is None
        saved = store.get_review_work_item(item.id)
        assert saved is not None
        assert saved.status == ReviewWorkItemStatus.PENDING_REVIEW
        assert saved.lifecycle_stage == ReviewLifecycleStage.REVIEW_FAILED
        assert saved.failure_count == 1
        assert saved.last_error == "mock lifecycle exception"

        failures = client.get("/debug/recent-failures").json()
        assert failures[0]["last_error"] == "mock lifecycle exception"


def test_disabled_feature_flags_do_not_attempt_external_services(tmp_path):
    with orchestrator_client(tmp_path) as (client, _settings, store):
        item = _review_item()
        store.save_review_work_item(item)

        response = client.post(
            f"/debug/review-queue/{item.id}/process",
            headers={"X-Orchestrator-Admin-Token": "ci-admin-token"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["openai_review_attempted"] is False
        assert body["github_writeback_attempted"] is False
        assert body["task_dispatch_attempted"] is False


def test_github_writeback_mocked_success_records_lifecycle_events(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "GitHubClient", lambda token: SuccessfulGitHubClient(token))
    monkeypatch.setattr(main_module, "dispatch_next_agent_task", _mock_task_dispatch)

    with orchestrator_client(tmp_path, enable_github_writeback=True, github_token="mock-token") as (client, _settings, store):
        item = _review_item()
        store.save_review_work_item(item)

        response = client.post(
            f"/debug/review-queue/{item.id}/process",
            headers={"X-Orchestrator-Admin-Token": "ci-admin-token"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["github_writeback_attempted"] is True
        assert body["github_writeback_success"] is True

        saved = store.get_review_work_item(item.id)
        assert saved is not None
        assert saved.github_writeback_started_at is not None
        assert saved.github_writeback_completed_at is not None
        assert saved.github_writeback_success is True
        assert saved.review_completed_at is not None


def test_github_writeback_mocked_failure_records_visible_error(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "GitHubClient", lambda token: FailingGitHubClient(token))
    monkeypatch.setattr(main_module, "dispatch_next_agent_task", _mock_task_dispatch)

    with orchestrator_client(tmp_path, enable_github_writeback=True, github_token="mock-token") as (client, _settings, store):
        item = _review_item()
        store.save_review_work_item(item)

        response = client.post(
            f"/debug/review-queue/{item.id}/process",
            headers={"X-Orchestrator-Admin-Token": "ci-admin-token"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["github_writeback_attempted"] is True
        assert body["github_writeback_success"] is False
        assert body["github_writeback_error"] == "mock writeback failure"

        saved = store.get_review_work_item(item.id)
        assert saved is not None
        assert saved.github_writeback_started_at is not None
        assert saved.github_writeback_completed_at is not None
        assert saved.github_writeback_success is False
        assert saved.last_error == "mock writeback failure"


class SuccessfulGitHubClient:
    def __init__(self, token):
        self.token = token

    async def post_issue_comment(self, repo_full_name, issue_number, body):
        return {"repo_full_name": repo_full_name, "issue_number": issue_number, "body": body}

    async def apply_label(self, repo_full_name, issue_number, label):
        return {"repo_full_name": repo_full_name, "issue_number": issue_number, "label": label}

    async def aclose(self):
        return None


class FailingGitHubClient(SuccessfulGitHubClient):
    async def apply_label(self, repo_full_name, issue_number, label):
        raise RuntimeError("mock writeback failure")


async def _mock_task_dispatch(*_args, **_kwargs):
    return SimpleNamespace(
        attempted=False,
        success=False,
        issue_number=None,
        error=None,
    )


def _review_item():
    now = datetime.now(UTC)
    return ReviewWorkItem(
        id=str(uuid4()),
        created_at=now,
        updated_at=now,
        repo_full_name="marcus937/riseos-agent-orchestrator",
        event_type=GitHubEventType.PULL_REQUEST,
        branch="circuit/test-branch",
        commit_sha="a" * 40,
        pr_number=42,
    )


def _pull_request_payload():
    return {
        "action": "opened",
        "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
        "sender": {"login": "circuit-forge"},
        "number": 42,
        "pull_request": {
            "number": 42,
            "head": {"sha": "a" * 40, "ref": "circuit/test-branch"},
            "base": {"ref": "main"},
            "labels": [],
        },
    }


def _signature(payload):
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(b"ci-webhook-secret", body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
