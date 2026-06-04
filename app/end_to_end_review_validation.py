from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.5
MOCK_GITHUB_BASE_URL = "http://127.0.0.1:9001"
MOCK_GITHUB_READY_PATH = "/__mock_github/ready"
MOCK_GITHUB_REQUESTS_PATH = "/__mock_github/requests"
DETERMINISTIC_REPO = "riseos/end-to-end-review-validation"
DETERMINISTIC_PR_NUMBER = 55
DETERMINISTIC_HEAD_SHA = "5555555555555555555555555555555555555555"


class ValidationError(RuntimeError):
    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    failed_stage: str | None = None
    detail: str | None = None


def sign_payload(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_pull_request_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"full_name": DETERMINISTIC_REPO},
        "sender": {"login": "end-to-end-review-validation"},
        "number": DETERMINISTIC_PR_NUMBER,
        "pull_request": {
            "number": DETERMINISTIC_PR_NUMBER,
            "head": {
                "ref": "circuit/phase-5-e2e-review-validation",
                "sha": DETERMINISTIC_HEAD_SHA,
            },
            "base": {"ref": "main"},
            "labels": [],
        },
    }


def build_mock_github_base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_lifecycle_summary(path: Path, result: ValidationResult, checks: dict[str, Any] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# End-to-End Review Validation", ""]
    if result.passed:
        lines.extend(["PASSED", "", "All required lifecycle stages completed through the running orchestrator."])
    else:
        failed_stage = result.failed_stage or "unknown"
        lines.extend(["FAILED", "", f"Failed stage: {failed_stage}", f"Detail: {result.detail or 'No detail provided.'}"])
    if checks:
        lines.extend(["", "## Checks"])
        for name, value in checks.items():
            lines.append(f"- {name}: {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 10.0,
) -> tuple[int, Any, str]:
    req = request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            payload = json.loads(text) if text else {}
            return response.status, payload, text
    except error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"raw": text}
        return exc.code, payload, text


def wait_for_json(
    stage: str,
    url: str,
    *,
    expected_status: int = 200,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    headers: dict[str, str] | None = None,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last_error = "not attempted"
    while time.monotonic() <= deadline:
        try:
            status_code, payload, text = request_json("GET", url, headers=headers, timeout=5.0)
            if status_code == expected_status:
                return payload
            last_error = f"HTTP {status_code}: {text}"
        except OSError as exc:
            last_error = str(exc)
        time.sleep(POLL_INTERVAL_SECONDS)
    raise ValidationError(stage, last_error)


def post_signed_pull_request(base_url: str, webhook_secret: str, artifacts_dir: Path) -> dict[str, Any]:
    payload = build_pull_request_payload()
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": sign_payload(webhook_secret, body),
    }
    status_code, response_payload, text = request_json("POST", f"{base_url}/webhooks/github", headers=headers, body=body)
    webhook_record = {
        "request": {"event": "pull_request", "payload": payload},
        "response": {"status_code": status_code, "body": response_payload, "raw": text},
    }
    write_json(artifacts_dir / "webhook.json", webhook_record)
    if status_code != 200:
        raise ValidationError("webhook_received", f"Webhook returned HTTP {status_code}: {text}")
    return response_payload


def admin_headers(admin_token: str) -> dict[str, str]:
    return {"X-Orchestrator-Admin-Token": admin_token, "Accept": "application/json"}


def get_debug_json(base_url: str, path: str, admin_token: str) -> Any:
    status_code, payload, text = request_json("GET", f"{base_url}{path}", headers=admin_headers(admin_token))
    if status_code != 200:
        raise ValidationError(path.strip("/") or "debug", f"HTTP {status_code}: {text}")
    return payload


def matching_queue_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in items:
        if (
            item.get("repo_full_name") == DETERMINISTIC_REPO
            and item.get("pr_number") == DETERMINISTIC_PR_NUMBER
            and item.get("commit_sha") == DETERMINISTIC_HEAD_SHA
        ):
            return item
    return None


def wait_for_queue_item(base_url: str, admin_token: str, artifacts_dir: Path) -> dict[str, Any]:
    deadline = time.monotonic() + DEFAULT_TIMEOUT_SECONDS
    last_items: list[dict[str, Any]] = []
    while time.monotonic() <= deadline:
        items = get_debug_json(base_url, "/debug/review-queue", admin_token)
        last_items = items if isinstance(items, list) else []
        item = matching_queue_item(last_items)
        if item is not None:
            write_json(artifacts_dir / "queue-state.json", {"matched_item": item, "all_items": last_items})
            return item
        time.sleep(POLL_INTERVAL_SECONDS)
    write_json(artifacts_dir / "queue-state.json", {"matched_item": None, "all_items": last_items})
    raise ValidationError("queue_entry_created", "No deterministic review queue item was found.")


def wait_for_lifecycle_field(base_url: str, admin_token: str, item_id: str, field: str, stage: str) -> dict[str, Any]:
    deadline = time.monotonic() + DEFAULT_TIMEOUT_SECONDS
    last_item: dict[str, Any] = {}
    while time.monotonic() <= deadline:
        lifecycle = get_debug_json(base_url, "/debug/review-lifecycle", admin_token)
        for item in lifecycle:
            if item.get("item_id") == item_id:
                last_item = item
                if item.get(field):
                    return item
        time.sleep(POLL_INTERVAL_SECONDS)
    raise ValidationError(stage, f"Lifecycle field {field} was not populated. Last item: {last_item}")


def fetch_mock_github_requests(mock_github_base_url: str) -> list[dict[str, Any]]:
    status_code, payload, text = request_json("GET", f"{mock_github_base_url}{MOCK_GITHUB_REQUESTS_PATH}")
    if status_code != 200:
        raise ValidationError("writeback_attempt", f"Mock GitHub request capture failed: HTTP {status_code}: {text}")
    if not isinstance(payload, list):
        raise ValidationError("writeback_attempt", "Mock GitHub request capture returned a non-list payload.")
    return payload


def assert_writeback_requests(requests: list[dict[str, Any]], expected_api_base_url: str) -> None:
    expected_prefix = f"/repos/{DETERMINISTIC_REPO}/issues/{DETERMINISTIC_PR_NUMBER}"
    comments = [item for item in requests if item.get("method") == "POST" and item.get("path") == f"{expected_prefix}/comments"]
    labels = [item for item in requests if item.get("method") == "POST" and item.get("path") == f"{expected_prefix}/labels"]
    if not comments:
        raise ValidationError("writeback_attempt", "No mock GitHub issue comment request was captured.")
    if not labels:
        raise ValidationError("writeback_attempt", "No mock GitHub label request was captured.")
    for item in [*comments, *labels]:
        if item.get("api_base_url") != expected_api_base_url:
            raise ValidationError("writeback_attempt", f"Unexpected API base URL evidence: {item.get('api_base_url')!r}")


def assert_lifecycle_complete(item: dict[str, Any]) -> None:
    expected = {
        "status": "approved_for_human_review",
        "lifecycle_stage": "review_completed",
        "github_writeback_success": True,
    }
    for field, value in expected.items():
        if item.get(field) != value:
            raise ValidationError("lifecycle_completion", f"Expected {field}={value!r}; found {item.get(field)!r}.")
    required_timestamps = [
        "queued_at",
        "worker_claimed_at",
        "review_started_at",
        "review_completed_at",
        "github_writeback_started_at",
        "github_writeback_completed_at",
    ]
    missing = [field for field in required_timestamps if not item.get(field)]
    if missing:
        raise ValidationError("lifecycle_completion", f"Missing lifecycle timestamp(s): {', '.join(missing)}")


def collect_diagnostics(base_url: str, admin_token: str, mock_github_base_url: str) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    for name, path in {
        "review_queue": "/debug/review-queue",
        "review_lifecycle": "/debug/review-lifecycle",
        "queue_stats": "/debug/review-queue/stats",
        "worker_stats": "/debug/workers/stats",
        "recent_events": "/debug/recent-events",
        "debug_health": "/debug/health",
        "recent_failures": "/debug/recent-failures",
    }.items():
        diagnostics[name] = get_debug_json(base_url, path, admin_token)
    diagnostics["mock_github_requests"] = fetch_mock_github_requests(mock_github_base_url)
    return diagnostics


def run_validation(base_url: str, webhook_secret: str, admin_token: str, artifacts_dir: Path, mock_github_base_url: str) -> int:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    checks: dict[str, Any] = {}
    try:
        mock_health = wait_for_json("mock_github_ready", f"{mock_github_base_url}{MOCK_GITHUB_READY_PATH}")
        checks["mock_github_ready"] = mock_health
        app_health = wait_for_json("app_readiness", f"{base_url}/health")
        checks["app_readiness"] = app_health
        post_signed_pull_request(base_url, webhook_secret, artifacts_dir)
        checks["webhook_received"] = True

        queued_item = wait_for_queue_item(base_url, admin_token, artifacts_dir)
        item_id = str(queued_item["id"])
        checks["queue_entry_created"] = item_id

        claimed_item = wait_for_lifecycle_field(base_url, admin_token, item_id, "worker_claimed_at", "worker_claim")
        write_json(artifacts_dir / "worker-claim.json", claimed_item)
        checks["worker_claim"] = claimed_item.get("worker_claimed_at")

        started_item = wait_for_lifecycle_field(base_url, admin_token, item_id, "review_started_at", "review_processing")
        checks["review_processing"] = started_item.get("review_started_at")

        completed_item = wait_for_lifecycle_field(base_url, admin_token, item_id, "review_completed_at", "lifecycle_completion")
        assert_lifecycle_complete(completed_item)
        write_json(artifacts_dir / "review-result.json", completed_item)
        checks["lifecycle_completion"] = completed_item.get("review_completed_at")

        mock_requests = fetch_mock_github_requests(mock_github_base_url)
        assert_writeback_requests(mock_requests, mock_github_base_url)
        write_json(artifacts_dir / "mock-github-requests.json", mock_requests)
        checks["writeback_attempt"] = len(mock_requests)

        diagnostics = collect_diagnostics(base_url, admin_token, mock_github_base_url)
        write_json(artifacts_dir / "diagnostics.json", diagnostics)
        write_lifecycle_summary(artifacts_dir / "lifecycle-summary.md", ValidationResult(passed=True), checks)
        return 0
    except ValidationError as exc:
        try:
            diagnostics = collect_diagnostics(base_url, admin_token, mock_github_base_url)
            write_json(artifacts_dir / "diagnostics.json", diagnostics)
            if not (artifacts_dir / "mock-github-requests.json").exists():
                write_json(artifacts_dir / "mock-github-requests.json", diagnostics.get("mock_github_requests", []))
        except Exception as diagnostics_error:
            write_json(artifacts_dir / "diagnostics.json", {"diagnostics_error": str(diagnostics_error)})
        write_lifecycle_summary(artifacts_dir / "lifecycle-summary.md", ValidationResult(False, exc.stage, exc.detail), checks)
        return 1


class MockGitHubHandler(BaseHTTPRequestHandler):
    records: list[dict[str, Any]] = []

    def do_GET(self) -> None:
        if self.path == MOCK_GITHUB_READY_PATH:
            response: Any = {"ok": True, "api_base_url": getattr(self.server, "api_base_url", MOCK_GITHUB_BASE_URL), "records": len(self.records)}
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
        record = {
            "method": "POST",
            "path": self.path,
            "api_base_url": getattr(self.server, "api_base_url", MOCK_GITHUB_BASE_URL),
            "body": body,
        }
        self.records.append(record)
        print(f"mock-github captured {record['method']} {record['path']}: {json.dumps(body, sort_keys=True)}", flush=True)
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "id": len(self.records), "path": self.path}).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        print(f"mock-github: {format % args}", flush=True)


def run_mock_github(host: str, port: int) -> None:
    MockGitHubHandler.records = []
    server = ThreadingHTTPServer((host, port), MockGitHubHandler)
    server.api_base_url = build_mock_github_base_url(host, port)
    print(f"mock-github listening on {server.api_base_url}", flush=True)
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run end-to-end review validation.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--base-url", required=True)
    validate.add_argument("--webhook-secret", required=True)
    validate.add_argument("--admin-token", required=True)
    validate.add_argument("--artifacts-dir", required=True, type=Path)
    validate.add_argument("--mock-github-base-url", default=MOCK_GITHUB_BASE_URL)

    mock = subparsers.add_parser("mock-github")
    mock.add_argument("--host", default="127.0.0.1")
    mock.add_argument("--port", default=9001, type=int)

    args = parser.parse_args()
    if args.command == "validate":
        return run_validation(
            args.base_url.rstrip("/"),
            args.webhook_secret,
            args.admin_token,
            args.artifacts_dir,
            args.mock_github_base_url.rstrip("/"),
        )
    if args.command == "mock-github":
        thread = threading.Thread(target=run_mock_github, args=(args.host, args.port), daemon=False)
        thread.start()
        thread.join()
        return 0
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
