from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx


DEFAULT_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.5


class ValidationError(RuntimeError):
    pass


def sign_payload(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_pull_request_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"full_name": "riseos/staging-validation"},
        "sender": {"login": "staging-validation"},
        "pull_request": {
            "number": 33,
            "head": {
                "ref": "circuit/staging-deployment-validation",
                "sha": "abc123def456abc123def456abc123def456abcd",
            },
            "base": {"ref": "main"},
            "labels": [],
        },
        "number": 33,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def wait_for_health(base_url: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{base_url}/health")
                if response.status_code == 200:
                    return response.json()
                last_error = f"HTTP {response.status_code}: {response.text}"
            except httpx.HTTPError as exc:
                last_error = str(exc)
            time.sleep(POLL_INTERVAL_SECONDS)
    raise ValidationError(f"Application did not become healthy: {last_error}")


def post_signed_pull_request(base_url: str, webhook_secret: str) -> dict[str, Any]:
    payload = json.dumps(build_pull_request_payload(), separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": sign_payload(webhook_secret, payload),
    }
    with httpx.Client(timeout=10.0) as client:
        response = client.post(f"{base_url}/webhooks/github", content=payload, headers=headers)
    if response.status_code != 200:
        raise ValidationError(f"Webhook was not accepted: HTTP {response.status_code}: {response.text}")
    return response.json()


def get_debug_json(base_url: str, path: str, admin_token: str) -> Any:
    headers = {"X-Orchestrator-Admin-Token": admin_token}
    with httpx.Client(timeout=10.0) as client:
        response = client.get(f"{base_url}{path}", headers=headers)
    if response.status_code != 200:
        raise ValidationError(f"Debug endpoint {path} failed: HTTP {response.status_code}: {response.text}")
    return response.json()


def wait_for_completed_review(base_url: str, admin_token: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    last_lifecycle: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        lifecycle = get_debug_json(base_url, "/debug/review-lifecycle", admin_token)
        last_lifecycle = lifecycle
        if lifecycle and all(item.get("lifecycle_stage") == "review_completed" for item in lifecycle):
            return lifecycle
        time.sleep(POLL_INTERVAL_SECONDS)
    raise ValidationError(f"Review lifecycle did not complete: {json.dumps(last_lifecycle, default=str)}")


def assert_staging_lifecycle(lifecycle: list[dict[str, Any]]) -> None:
    if len(lifecycle) != 1:
        raise ValidationError(f"Expected exactly one review work item, found {len(lifecycle)}.")
    item = lifecycle[0]
    expected_fields = {
        "status": "approved_for_human_review",
        "lifecycle_stage": "review_completed",
        "github_writeback_success": True,
    }
    for field, expected in expected_fields.items():
        if item.get(field) != expected:
            raise ValidationError(f"Expected {field}={expected!r}, found {item.get(field)!r}.")
    for field in [
        "worker_claimed_at",
        "review_started_at",
        "review_completed_at",
        "github_writeback_started_at",
        "github_writeback_completed_at",
    ]:
        if not item.get(field):
            raise ValidationError(f"Lifecycle field {field} was not populated.")


def run_validation(base_url: str, webhook_secret: str, admin_token: str, artifacts_dir: Path) -> None:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    health = wait_for_health(base_url)
    write_json(artifacts_dir / "health.json", health)

    webhook_response = post_signed_pull_request(base_url, webhook_secret)
    write_json(artifacts_dir / "webhook-response.json", webhook_response)

    lifecycle = wait_for_completed_review(base_url, admin_token)
    assert_staging_lifecycle(lifecycle)

    snapshots = {
        "review_queue": get_debug_json(base_url, "/debug/review-queue", admin_token),
        "review_lifecycle": lifecycle,
        "queue_stats": get_debug_json(base_url, "/debug/review-queue/stats", admin_token),
        "worker_stats": get_debug_json(base_url, "/debug/workers/stats", admin_token),
        "recent_events": get_debug_json(base_url, "/debug/recent-events", admin_token),
        "debug_health": get_debug_json(base_url, "/debug/health", admin_token),
        "recent_failures": get_debug_json(base_url, "/debug/recent-failures", admin_token),
    }
    write_json(artifacts_dir / "diagnostics.json", snapshots)


class MockGitHubHandler(BaseHTTPRequestHandler):
    records: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            body = {"raw": raw_body.decode("utf-8", errors="replace")}
        record = {"method": "POST", "path": self.path, "body": body}
        self.records.append(record)
        response = {"id": len(self.records), "ok": True, "path": self.path}
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        print(f"mock-github: {format % args}", flush=True)


def run_mock_github(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), MockGitHubHandler)
    print(f"mock-github listening on http://{host}:{port}", flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run staging deployment validation.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--base-url", required=True)
    validate.add_argument("--webhook-secret", required=True)
    validate.add_argument("--admin-token", required=True)
    validate.add_argument("--artifacts-dir", required=True, type=Path)

    mock = subparsers.add_parser("mock-github")
    mock.add_argument("--host", default="127.0.0.1")
    mock.add_argument("--port", default=9001, type=int)

    args = parser.parse_args()
    if args.command == "validate":
        run_validation(args.base_url.rstrip("/"), args.webhook_secret, args.admin_token, args.artifacts_dir)
    elif args.command == "mock-github":
        thread = threading.Thread(target=run_mock_github, args=(args.host, args.port), daemon=False)
        thread.start()
        thread.join()


if __name__ == "__main__":
    main()
