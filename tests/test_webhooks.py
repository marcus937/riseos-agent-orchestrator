import json

from fastapi.testclient import TestClient

from app.config import get_settings
from app.github_events import GitHubEventType, parse_github_event
from app.main import app
from app.security import build_signature


def client_with_secret(secret: str = "test-secret") -> TestClient:
    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: get_settings().__class__(github_webhook_secret=secret)
    return TestClient(app)


def signed_headers(secret: str, event: str, payload: bytes) -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": build_signature(secret, payload),
        "Content-Type": "application/json",
    }


def test_health_returns_ok() -> None:
    client = client_with_secret()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_signed_issue_comment_payload_is_accepted() -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    payload = {
        "action": "created",
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "marcus"},
        "issue": {"number": 7, "labels": [{"name": "agent:ready"}]},
    }
    body = json.dumps(payload).encode("utf-8")

    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "issue_comment", body))

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["event_type"] == "issue_comment"


def test_unsigned_payload_is_rejected() -> None:
    client = client_with_secret()
    response = client.post("/webhooks/github", json={"action": "created"}, headers={"X-GitHub-Event": "issue_comment"})
    assert response.status_code == 401


def test_invalid_signature_is_rejected() -> None:
    client = client_with_secret()
    response = client.post(
        "/webhooks/github",
        json={"action": "created"},
        headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": "sha256=bad"},
    )
    assert response.status_code == 401


def test_issue_comment_parser_extracts_context() -> None:
    parsed = parse_github_event(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "riseos/example"},
            "sender": {"login": "marcus"},
            "issue": {
                "number": 12,
                "pull_request": {"url": "https://api.github.com/repos/riseos/example/pulls/12"},
                "labels": [{"name": "agent:working"}],
            },
        },
    )

    assert parsed.event_type == GitHubEventType.ISSUE_COMMENT
    assert parsed.action == "created"
    assert parsed.repository == "riseos/example"
    assert parsed.sender == "marcus"
    assert parsed.issue_number == 12
    assert parsed.pull_request_number == 12
    assert parsed.labels == ["agent:working"]


def test_push_parser_extracts_context() -> None:
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "sender": {"login": "marcus"},
            "ref": "refs/heads/agent-integration",
            "before": "abc123",
            "after": "def456",
        },
    )

    assert parsed.event_type == GitHubEventType.PUSH
    assert parsed.repository == "riseos/example"
    assert parsed.sender == "marcus"
    assert parsed.ref == "refs/heads/agent-integration"
    assert parsed.before == "abc123"
    assert parsed.after == "def456"
    assert parsed.head_sha == "def456"


def test_pull_request_parser_extracts_context() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "number": 9,
            "repository": {"full_name": "riseos/example"},
            "sender": {"login": "marcus"},
            "pull_request": {
                "number": 9,
                "head": {"sha": "feedface"},
                "labels": [{"name": "agent:review-needed"}],
            },
        },
    )

    assert parsed.event_type == GitHubEventType.PULL_REQUEST
    assert parsed.action == "opened"
    assert parsed.repository == "riseos/example"
    assert parsed.sender == "marcus"
    assert parsed.pull_request_number == 9
    assert parsed.head_sha == "feedface"
    assert parsed.labels == ["agent:review-needed"]


def test_signed_agent_integration_push_returns_review_stub() -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    payload = {
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "agent"},
        "ref": "refs/heads/agent-integration",
        "after": "abc123",
    }
    body = json.dumps(payload).encode("utf-8")

    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "push", body))

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert data["event_accepted"] is True
    assert data["task_state"] == "review_needed"
    assert data["repo"] == "riseos/example"
    assert data["commit_sha"] == "abc123"
    assert data["review_context"]["trigger"] == "push_agent_integration"
