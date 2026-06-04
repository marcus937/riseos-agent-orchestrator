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
from urllib import error, request


DEFAULT_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.5
MOCK_GITHUB_BASE_URL = "http://127.0.0.1:9001"
MOCK_GITHUB_READY_PATH = "/__mock_github/ready"
MOCK_GITHUB_REQUESTS_PATH = "/__mock_github/requests"


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
                "ref": "agent-integration",
                "sha": "abc123def456abc123def456abc123def456abcd",
            },
            "base": {"ref": "main"},
            "labels": [],
        },
        "number": 33,
    }


def build_mock_github_base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def request_json(method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None, timeout: float = 10.0) -> tuple[int, Any, str]:
    req = request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8")
            payload = json.loads(text) if text else {}
            return response.status, payload, text
    except error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"raw": text}
        return exc.code, payload, text


def wait_for_health(base_url: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            status_code, payload, text = request_json("GET", f"{base_url}/health", timeout=5.0)
            if status_code == 200:
                return payload
            last_error = f"HTTP {status_code}: {text}"
        except OSError as exc:
            last_error = str(exc)
        time.sleep(POLL_INTERVAL_SECONDS)
    raise ValidationError(f"Application did not become healthy: {last_error}")


def wait_for_mock_github(mock_github_base_url: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            status_code, payload, text = request_json("GET", f"{mock_github_base_url}{MOCK_GITHUB_READY_PATH}", timeout=5.0)
            if status_code == 200:
                if payload.get("ok") is True:
                    return payload
                last_error = f"readiness payload was not ok: {payload}"
            else:
                last_error = f"HTTP {status_code}: {text}"
        except OSError as exc:
            last_error = str(exc)
        time.sleep(POLL_INTERVAL_SECONDS)
    raise ValidationError(f"Mock GitHub API did not become ready: {last_error}")


def post_signed_pull_request(base_url: str, webhook_secret: str) -> dict[str, Any]:
    payload = json.dumps(build_pull_request_payload(), separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": sign_payload(webhook_secret, payload),
    }
    status_code, response_payload, text = request_json("POST", f"{base_url}/webhooks/github", body=payload, headers=headers)
    if status_code != 200:
        raise ValidationError(f"Webhook was not accepted: HTTP {status_code}: {text}")
    return response_payload


def get_debug_json(base_url: str, path: str, admin_token: str) -> Any:
    headers = {"X-Orchestrator-Admin-Token": admin_token}
    status_code, payload, text = request_json("GET", f"{base_url}{path}", headers=headers)
    if status_code != 200:
        raise ValidationError(f"Debug endpoint {path} failed: HTTP {status_code}: {text}")
    return payload


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


def fetch_mock_github_requests(mock_github_base_url: str) -> list[dict[str, Any]]:
    status_code, payload, text = request_json("GET", f"{mock_github_base_url}{MOCK_GITHUB_REQUESTS_PATH}")
    if status_code != 200:
        raise ValidationError(f"Mock GitHub request capture failed: HTTP {status_code}: {text}")
    if not isinstance(payload, list):
        raise ValidationError("Mock GitHub request capture returned a non-list payload.")
    return payload


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


def assert_mock_github_writeback(requests: list[dict[str, Any]], expected_api_base_url: str = MOCK_GITHUB_BASE_URL) -> None:
    writeback_requests = [
        request
        for request in requests
        if request.get("method") == "POST"
        and request.get("path", "").startswith("/repos/riseos/staging-validation/issues/33/")
    ]
    if not writeback_requests:
        raise ValidationError("Mock GitHub did not receive the expected issue writeback request.")
    for request in writeback_requests:
        if request.get("api_base_url") != expected_api_base_url:
            raise ValidationError(f"Unexpected GitHub API base URL evidence: {request.get('api_base_url')!r}.")


def run_validation(base_url: str, webhook_secret: str, admin_token: str, artifacts_dir: Path, mock_github_base_url: str) -> None:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    mock_github_health = wait_for_mock_github(mock_github_base_url)
    write_json(artifacts_dir / "mock-github-health.json", mock_github_health)

    health = wait_for_health(base_url)
    write_json(artifacts_dir / "health.json", health)

    webhook_response = post_signed_pull_request(base_url, webhook_secret)
    write_json(artifacts_dir / "webhook-response.json", webhook_response)

    lifecycle = wait_for_completed_review(base_url, admin_token)
    assert_staging_lifecycle(lifecycle)

    mock_github_requests = fetch_mock_github_requests(mock_github_base_url)
    assert_mock_github_writeback(mock_github_requests, mock_github_base_url)
    write_json(artifacts_dir / "mock-github-requests.json", mock_github_requests)

    snapshots = {
        "review_queue": get_debug_json(base_url, "/debug/review-queue", admin_token),
        "review_lifecycle": lifecycle,
        "queue_stats": get_debug_json(base_url, "/debug/review-queue/stats", admin_token),
        "worker_stats": get_debug_json(base_url, "/debug/workers/stats", admin_token),
        "recent_events": get_debug_json(base_url, "/debug/recent-events", admin_token),
        "debug_health": get_debug_json(base_url, "/debug/health", admin_token),
        "recent_failures": get_debug_json(base_url, "/debug/recent-failures", admin_token),
        "mock_github_health": mock_github_health,
        "mock_github_writeback_requests": mock_github_requests,
    }
    write_json(artifacts_dir / "diagnostics.json", snapshots)


class MockGitHubHandler(BaseHTTPRequestHandler):
    records: list[dict[str, Any]] = []

    def do_GET(self) -> None:
        if self.path == MOCK_GITHUB_READY_PATH:
            api_base_url = getattr(self.server, "api_base_url", MOCK_GITHUB_BASE_URL)
            response = {"ok": True, "api_base_url": api_base_url, "records": len(self.records)}
        elif self.path == MOCK_GITHUB_REQUESTS_PATH:
            response = self.records
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            body = {"raw": raw_body.decode("utf-8", errors="replace")}
        api_base_url = getattr(self.server, "api_base_url", MOCK_GITHUB_BASE_URL)
        record = {
            "method": "POST",
            "path": self.path,
            "api_base_url": api_base_url,
            "body": body,
        }
        self.records.append(record)
        print(f"mock-github captured {record['method']} {record['path']}: {json.dumps(body, sort_keys=True)}", flush=True)
        response = {"id": len(self.records), "ok": True, "path": self.path}
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        print(f"mock-github: {format % args}", flush=True)


def run_mock_github(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), MockGitHubHandler)
    server.api_base_url = build_mock_github_base_url(host, port)
    print(f"mock-github listening on {server.api_base_url}", flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run staging deployment validation.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--base-url", required=True)
    validate.add_argument("--webhook-secret", required=True)
    validate.add_argument("--admin-token", required=True)
    validate.add_argument("--artifacts-dir", required=True, type=Path)
    validate.add_argument("--mock-github-base-url", default=MOCK_GITHUB_BASE_URL)

    wait_mock = subparsers.add_parser("wait-mock-github")
    wait_mock.add_argument("--mock-github-base-url", default=MOCK_GITHUB_BASE_URL)
    wait_mock.add_argument("--artifacts-dir", required=True, type=Path)

    mock = subparsers.add_parser("mock-github")
    mock.add_argument("--host", default="127.0.0.1")
    mock.add_argument("--port", default=9001, type=int)

    args = parser.parse_args()
    if args.command == "validate":
        run_validation(
            args.base_url.rstrip("/"),
            args.webhook_secret,
            args.admin_token,
            args.artifacts_dir,
            args.mock_github_base_url.rstrip("/"),
        )
    elif args.command == "wait-mock-github":
        health = wait_for_mock_github(args.mock_github_base_url.rstrip("/"))
        write_json(args.artifacts_dir / "mock-github-health.json", health)
    elif args.command == "mock-github":
        thread = threading.Thread(target=run_mock_github, args=(args.host, args.port), daemon=False)
        thread.start()
        thread.join()


if __name__ == "__main__":
    main()
