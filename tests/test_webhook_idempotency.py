import json
from typing import Any

from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings, get_settings
from app.event_store import event_store
from app.main import app
from app.review_queue import review_queue
from app.security import build_signature
from app.slack_issue_dispatch import SlackIssueDispatchResult


def signed_headers(secret: str, event: str, payload: bytes, *, delivery: str = "delivery-1") -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": build_signature(secret, payload),
        "Content-Type": "application/json",
    }


def client_with_sqlite(tmp_path: Any, secret: str = "test-secret") -> TestClient:
    get_settings.cache_clear()
    event_store.reset()
    review_queue.reset()
    db_path = tmp_path / "orchestrator.db"
    app.dependency_overrides[get_settings] = lambda: Settings(
        github_webhook_secret=secret,
        orchestrator_db_path=str(db_path),
        slack_webhook_url="https://hooks.slack.test/services/test",
    )
    return TestClient(app)


def test_duplicate_github_delivery_id_skips_slack_dispatch(monkeypatch: Any, tmp_path: Any) -> None:
    secret = "test-secret"
    dispatched: list[tuple[str | None, int | None]] = []

    async def fake_dispatch(parsed: Any, settings: Settings, **_: Any) -> SlackIssueDispatchResult:
        dispatched.append((parsed.repository, parsed.issue_number))
        return SlackIssueDispatchResult(attempted=True, success=True, issue_key=f"{parsed.repository}#{parsed.issue_number}")

    monkeypatch.setattr(main_module, "dispatch_ready_issue_to_slack", fake_dispatch)
    payload = {
        "action": "opened",
        "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
        "issue": {
            "number": 45,
            "title": "Agent Claim Deduplication",
            "state": "open",
            "html_url": "https://github.com/marcus937/riseos-agent-orchestrator/issues/45",
            "labels": [{"name": "agent-ready"}],
        },
        "sender": {"login": "marcus"},
    }
    body = json.dumps(payload).encode("utf-8")

    with client_with_sqlite(tmp_path, secret) as client:
        first = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "issues", body, delivery="same-delivery"))
        second = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "issues", body, delivery="same-delivery"))
        events = client.get("/debug/recent-events").json()

    assert first.status_code == 200
    assert second.status_code == 200
    assert dispatched == [("marcus937/riseos-agent-orchestrator", 45)]
    assert len(events) == 1
    assert events[0]["event_id"] == "github-delivery:same-delivery"


def test_duplicate_github_delivery_id_skips_queue_creation(tmp_path: Any) -> None:
    secret = "test-secret"
    payload = {
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "agent"},
        "ref": "refs/heads/agent-integration",
        "after": "abc123",
    }
    body = json.dumps(payload).encode("utf-8")

    with client_with_sqlite(tmp_path, secret) as client:
        first = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "push", body, delivery="same-push"))
        second = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "push", body, delivery="same-push"))
        queue = client.get("/debug/review-queue").json()
        events = client.get("/debug/recent-events").json()

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(queue) == 1
    assert len(events) == 1
    assert events[0]["event_id"] == "github-delivery:same-push"
