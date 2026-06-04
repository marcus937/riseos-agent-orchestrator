import subprocess
import sys

from app.runtime_validation import (
    RuntimeEndpoint,
    RuntimeHttpResponse,
    build_url,
    check_endpoint,
    graceful_shutdown,
    run_smoke_validation,
    wait_for_readiness,
)


def test_build_url_normalizes_slashes() -> None:
    assert build_url("http://127.0.0.1:8000/", "/health") == "http://127.0.0.1:8000/health"


def test_wait_for_readiness_retries_until_success() -> None:
    responses = [
        RuntimeHttpResponse(None, "", 0.01, "connection refused"),
        RuntimeHttpResponse(503, "starting", 0.01, None),
        RuntimeHttpResponse(200, '{"status":"ok"}', 0.01, None),
    ]
    seen_urls: list[str] = []

    def fake_request(url: str, timeout_seconds: float) -> RuntimeHttpResponse:
        seen_urls.append(url)
        return responses.pop(0)

    response = wait_for_readiness(
        "http://service/health",
        timeout_seconds=5,
        interval_seconds=0,
        request_fn=fake_request,
    )

    assert response.status_code == 200
    assert seen_urls == ["http://service/health"] * 3


def test_check_endpoint_returns_passed_result() -> None:
    def fake_request(url: str, timeout_seconds: float) -> RuntimeHttpResponse:
        return RuntimeHttpResponse(200, '{"items":[]}', 0.02, None)

    result = check_endpoint(
        "http://service",
        RuntimeEndpoint("review_queue", "/debug/review-queue"),
        request_fn=fake_request,
    )

    assert result.passed is True
    assert result.url == "http://service/debug/review-queue"


def test_run_smoke_validation_writes_response_artifacts(tmp_path) -> None:
    def fake_request(url: str, timeout_seconds: float) -> RuntimeHttpResponse:
        return RuntimeHttpResponse(200, '{"ok":true}', 0.01, None)

    exit_code = run_smoke_validation(
        base_url="http://service",
        artifact_dir=tmp_path,
        readiness_timeout_seconds=5,
        endpoints=(RuntimeEndpoint("health", "/health"),),
        request_fn=fake_request,
    )

    assert exit_code == 0
    assert (tmp_path / "startup-readiness.json").exists()
    assert (tmp_path / "http-responses" / "health.json").exists()
    assert "All runtime smoke checks passed." in (tmp_path / "failure-summary.md").read_text(encoding="utf-8")


def test_run_smoke_validation_reports_failed_endpoint(tmp_path) -> None:
    def fake_request(url: str, timeout_seconds: float) -> RuntimeHttpResponse:
        if url.endswith("/health"):
            return RuntimeHttpResponse(200, '{"status":"ok"}', 0.01, None)
        return RuntimeHttpResponse(404, "missing", 0.01, "HTTP 404")

    exit_code = run_smoke_validation(
        base_url="http://service",
        artifact_dir=tmp_path,
        readiness_timeout_seconds=5,
        endpoints=(RuntimeEndpoint("diagnostics", "/debug/health"),),
        request_fn=fake_request,
    )

    assert exit_code == 1
    summary = (tmp_path / "failure-summary.md").read_text(encoding="utf-8")
    assert "diagnostics" in summary
    assert "got 404" in summary


def test_graceful_shutdown_terminates_process() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert graceful_shutdown(process, timeout_seconds=5) is True
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
