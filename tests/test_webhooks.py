import json

from fastapi.testclient import TestClient

from app.config import get_settings
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
