import json

from fastapi.testclient import TestClient

from app.config import get_settings
from app.event_store import event_store
from app.github_events import GitHubEventType, parse_github_event
from app.main import app
from app.review_queue import review_queue
from app.security import build_signature


def client_with_secret(
    secret: str = "test-secret",
    admin_token: str = "admin-token",
    require_debug_read_token: bool = False,
) -> TestClient:
    get_settings.cache_clear()
    event_store.reset()
    review_queue.reset()
    app.dependency_overrides[get_settings] = lambda: get_settings().__class__(
        github_webhook_secret=secret,
        orchestrator_admin_token=admin_token,
        require_admin_token_for_debug_reads=require_debug_read_token,
    )
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


def test_debug_health_counts_rejected_webhook() -> None:
    client = client_with_secret()

    response = client.post(
        "/webhooks/github",
        json={"action": "created"},
        headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": "sha256=bad"},
    )

    assert response.status_code == 401
    debug = client.get("/debug/health").json()
    assert debug["webhook_count"] == 1
    assert debug["accepted_count"] == 0
    assert debug["rejected_count"] == 1
    assert debug["review_queue_count"] == 0
    assert debug["pending_review_count"] == 0
    assert debug["approved_count"] == 0
    assert debug["uptime"] >= 0


def test_debug_read_endpoints_are_public_when_flag_false() -> None:
    secret = "test-secret"
    client = client_with_secret(secret, require_debug_read_token=False)
    payload = {
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "agent"},
        "ref": "refs/heads/agent-integration",
        "after": "abc123",
    }
    body = json.dumps(payload).encode("utf-8")
    client.post("/webhooks/github", content=body, headers=signed_headers(secret, "push", body))
    item = client.get("/debug/review-queue").json()[0]

    assert client.get("/debug/health").status_code == 200
    assert client.get("/debug/recent-events").status_code == 200
    assert client.get("/debug/review-queue").status_code == 200
    assert client.get(f"/debug/review-queue/{item['id']}").status_code == 200


def test_debug_read_endpoints_reject_missing_token_when_flag_true() -> None:
    secret = "test-secret"
    client = client_with_secret(secret, require_debug_read_token=True)
    payload = {
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "agent"},
        "ref": "refs/heads/agent-integration",
        "after": "abc123",
    }
    body = json.dumps(payload).encode("utf-8")
    client.post("/webhooks/github", content=body, headers=signed_headers(secret, "push", body))

    assert client.get("/debug/health").status_code == 401
    assert client.get("/debug/recent-events").status_code == 401
    assert client.get("/debug/review-queue").status_code == 401
    assert client.get("/debug/review-queue/missing").status_code == 401


def test_debug_read_endpoints_accept_valid_token_when_flag_true() -> None:
    secret = "test-secret"
    client = client_with_secret(secret, require_debug_read_token=True)
    payload = {
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "agent"},
        "ref": "refs/heads/agent-integration",
        "after": "abc123",
    }
    body = json.dumps(payload).encode("utf-8")
    client.post("/webhooks/github", content=body, headers=signed_headers(secret, "push", body))
    headers = {"X-Orchestrator-Admin-Token": "admin-token"}
    item = client.get("/debug/review-queue", headers=headers).json()[0]

    assert client.get("/debug/health", headers=headers).status_code == 200
    assert client.get("/debug/recent-events", headers=headers).status_code == 200
    assert client.get("/debug/review-queue", headers=headers).status_code == 200
    assert client.get(f"/debug/review-queue/{item['id']}", headers=headers).status_code == 200


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


def test_accepted_webhook_is_stored_in_recent_events() -> None:
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
    events = client.get("/debug/recent-events").json()
    assert len(events) == 1
    assert events[0]["event_id"]
    assert events[0]["github_event"] == "push"
    assert events[0]["repo_full_name"] == "riseos/example"
    assert events[0]["branch"] == "agent-integration"
    assert events[0]["commit_sha"] == "abc123"
    assert events[0]["issue_number"] is None
    assert events[0]["pr_number"] is None
    assert events[0]["raw_action"] is None

    debug = client.get("/debug/health").json()
    assert debug["webhook_count"] == 1
    assert debug["accepted_count"] == 1
    assert debug["rejected_count"] == 0


def test_agent_integration_push_creates_review_queue_item() -> None:
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
    queue = client.get("/debug/review-queue").json()
    assert len(queue) == 1
    item = queue[0]
    assert item["id"]
    assert item["repo_full_name"] == "riseos/example"
    assert item["event_type"] == "push"
    assert item["branch"] == "agent-integration"
    assert item["commit_sha"] == "abc123"
    assert item["issue_number"] is None
    assert item["pr_number"] is None
    assert item["status"] == "pending_review"

    lookup = client.get(f"/debug/review-queue/{item['id']}")
    assert lookup.status_code == 200
    assert lookup.json() == item

    debug = client.get("/debug/health").json()
    assert debug["review_queue_count"] == 1
    assert debug["pending_review_count"] == 1


def test_status_done_issue_comment_creates_review_queue_item() -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    payload = {
        "action": "created",
        "repository": {"full_name": "riseos/example"},
        "issue": {"number": 42},
        "comment": {"body": "Status: Done\nReady for review."},
        "sender": {"login": "agent"},
    }
    body = json.dumps(payload).encode("utf-8")

    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "issue_comment", body))

    assert response.status_code == 200
    queue = client.get("/debug/review-queue").json()
    assert len(queue) == 1
    assert queue[0]["event_type"] == "issue_comment"
    assert queue[0]["repo_full_name"] == "riseos/example"
    assert queue[0]["issue_number"] == 42
    assert queue[0]["pr_number"] is None
    assert queue[0]["status"] == "pending_review"


def test_status_done_issue_comment_with_commit_line_sets_commit_sha() -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    commit_sha = "11ff7f7fad1b5c563e42f143eb9523c3126974cf5"
    payload = {
        "action": "created",
        "repository": {"full_name": "riseos/example"},
        "issue": {"number": 42},
        "comment": {"body": f"Status: Done\nCommit: {commit_sha}"},
        "sender": {"login": "agent"},
    }
    body = json.dumps(payload).encode("utf-8")

    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "issue_comment", body))

    assert response.status_code == 200
    assert response.json()["commit_sha"] == commit_sha
    queue = client.get("/debug/review-queue").json()
    assert queue[0]["commit_sha"] == commit_sha
    events = client.get("/debug/recent-events").json()
    assert events[0]["commit_sha"] == commit_sha


def test_status_done_issue_comment_with_lowercase_commit_short_sha_sets_commit_sha() -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    payload = {
        "action": "created",
        "repository": {"full_name": "riseos/example"},
        "issue": {"number": 42},
        "comment": {"body": "Status: Done\ncommit: abc1234"},
        "sender": {"login": "agent"},
    }
    body = json.dumps(payload).encode("utf-8")

    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "issue_comment", body))

    assert response.status_code == 200
    assert client.get("/debug/review-queue").json()[0]["commit_sha"] == "abc1234"


def test_status_done_issue_comment_with_commit_sha_format_sets_commit_sha() -> None:
    parsed = parse_github_event(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "riseos/example"},
            "issue": {"number": 42},
            "comment": {"body": "Status: Done\ncommit_sha: deadbee"},
            "sender": {"login": "agent"},
        },
    )

    assert parsed.head_sha == "deadbee"


def test_issue_comment_commit_sha_parser_supports_expected_labels() -> None:
    formats = [
        ("Commit", "abc1234"),
        ("commit", "abc1235"),
        ("SHA", "abc1236"),
        ("sha", "abc1237"),
        ("Commit SHA", "abc1238"),
        ("commit_sha", "abc1239"),
    ]

    for label, expected_sha in formats:
        parsed = parse_github_event(
            "issue_comment",
            {
                "action": "created",
                "repository": {"full_name": "riseos/example"},
                "issue": {"number": 42},
                "comment": {"body": f"Status: Done\n{label}: {expected_sha}"},
                "sender": {"login": "agent"},
            },
        )
        assert parsed.head_sha == expected_sha


def test_status_done_issue_comment_without_commit_line_keeps_commit_sha_null() -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    payload = {
        "action": "created",
        "repository": {"full_name": "riseos/example"},
        "issue": {"number": 42},
        "comment": {"body": "Status: Done\nReady for review."},
        "sender": {"login": "agent"},
    }
    body = json.dumps(payload).encode("utf-8")

    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "issue_comment", body))

    assert response.status_code == 200
    assert response.json()["commit_sha"] is None
    assert client.get("/debug/review-queue").json()[0]["commit_sha"] is None


def test_status_done_issue_comment_with_invalid_non_hex_commit_is_ignored() -> None:
    parsed = parse_github_event(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "riseos/example"},
            "issue": {"number": 42},
            "comment": {"body": "Status: Done\nSHA: not-a-sha"},
            "sender": {"login": "agent"},
        },
    )

    assert parsed.head_sha is None


def test_pull_request_opened_creates_review_queue_item() -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    payload = {
        "action": "opened",
        "repository": {"full_name": "riseos/example"},
        "pull_request": {
            "number": 7,
            "head": {"ref": "feature/task", "sha": "def456"},
            "base": {"ref": "main"},
        },
    }
    body = json.dumps(payload).encode("utf-8")

    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "pull_request", body))

    assert response.status_code == 200
    queue = client.get("/debug/review-queue").json()
    assert len(queue) == 1
    assert queue[0]["event_type"] == "pull_request"
    assert queue[0]["branch"] == "feature/task"
    assert queue[0]["commit_sha"] == "def456"
    assert queue[0]["pr_number"] == 7
    assert queue[0]["status"] == "pending_review"


def test_pull_request_synchronize_creates_review_queue_item() -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    payload = {
        "action": "synchronize",
        "repository": {"full_name": "riseos/example"},
        "pull_request": {
            "number": 8,
            "head": {"ref": "agent-integration", "sha": "feedface"},
            "base": {"ref": "main"},
        },
    }
    body = json.dumps(payload).encode("utf-8")

    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "pull_request", body))

    assert response.status_code == 200
    queue = client.get("/debug/review-queue").json()
    assert len(queue) == 1
    assert queue[0]["event_type"] == "pull_request"
    assert queue[0]["branch"] == "agent-integration"
    assert queue[0]["commit_sha"] == "feedface"
    assert queue[0]["pr_number"] == 8


def test_non_matching_accepted_event_does_not_create_review_queue_item() -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    payload = {
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "agent"},
        "ref": "refs/heads/main",
        "after": "abc123",
    }
    body = json.dumps(payload).encode("utf-8")

    response = client.post("/webhooks/github", content=body, headers=signed_headers(secret, "push", body))

    assert response.status_code == 200
    assert client.get("/debug/review-queue").json() == []
    debug = client.get("/debug/health").json()
    assert debug["accepted_count"] == 1
    assert debug["review_queue_count"] == 0


def test_missing_review_queue_item_returns_404() -> None:
    client = client_with_secret()

    response = client.get("/debug/review-queue/missing")

    assert response.status_code == 404


def test_process_review_queue_item_endpoint_updates_status() -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    payload = {
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "agent"},
        "ref": "refs/heads/agent-integration",
        "after": "abc123",
    }
    body = json.dumps(payload).encode("utf-8")
    client.post("/webhooks/github", content=body, headers=signed_headers(secret, "push", body))
    item = client.get("/debug/review-queue").json()[0]

    response = client.post(
        f"/debug/review-queue/{item['id']}/process",
        headers={"X-Orchestrator-Admin-Token": "admin-token"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["dry_run"] is True
    assert data["work_item"]["id"] == item["id"]
    assert data["work_item"]["status"] == "approved_for_human_review"
    assert data["decision"]["decision"] == "APPROVED_FOR_HUMAN_REVIEW"
    assert data["decision"]["summary"] == "Dry-run review processor accepted this work item for human review."
    assert "Do not merge automatically." in data["intended_next_actions"]

    lookup = client.get(f"/debug/review-queue/{item['id']}").json()
    assert lookup["status"] == "approved_for_human_review"
    debug = client.get("/debug/health").json()
    assert debug["pending_review_count"] == 0
    assert debug["approved_count"] == 1
    assert debug["approved_for_human_review_count"] == 1


def test_process_missing_review_queue_item_returns_404() -> None:
    client = client_with_secret()

    response = client.post(
        "/debug/review-queue/missing/process",
        headers={"X-Orchestrator-Admin-Token": "admin-token"},
    )

    assert response.status_code == 404


def test_process_endpoint_rejects_missing_admin_token() -> None:
    client = client_with_secret()

    response = client.post("/debug/review-queue/missing/process")

    assert response.status_code == 401


def test_process_endpoint_rejects_invalid_admin_token() -> None:
    client = client_with_secret()

    response = client.post(
        "/debug/review-queue/missing/process",
        headers={"X-Orchestrator-Admin-Token": "wrong"},
    )

    assert response.status_code == 401


def test_duplicate_webhook_does_not_create_duplicate_pending_queue_item() -> None:
    secret = "test-secret"
    client = client_with_secret(secret)
    payload = {
        "repository": {"full_name": "riseos/example"},
        "sender": {"login": "agent"},
        "ref": "refs/heads/agent-integration",
        "after": "abc123",
    }
    body = json.dumps(payload).encode("utf-8")
    headers = signed_headers(secret, "push", body)

    first = client.post("/webhooks/github", content=body, headers=headers)
    second = client.post("/webhooks/github", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    queue = client.get("/debug/review-queue").json()
    assert len(queue) == 1
    assert queue[0]["commit_sha"] == "abc123"
