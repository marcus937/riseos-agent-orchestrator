from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class RuntimeEndpoint:
    name: str
    path: str
    expected_status: int = 200


@dataclass(frozen=True, slots=True)
class RuntimeHttpResponse:
    status_code: int | None
    body: str
    elapsed_seconds: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None


@dataclass(frozen=True, slots=True)
class RuntimeCheckResult:
    endpoint: RuntimeEndpoint
    url: str
    response: RuntimeHttpResponse

    @property
    def passed(self) -> bool:
        return self.response.status_code == self.endpoint.expected_status and self.response.error is None


DEFAULT_SMOKE_ENDPOINTS: tuple[RuntimeEndpoint, ...] = (
    RuntimeEndpoint("health", "/health"),
    RuntimeEndpoint("diagnostics", "/debug/health"),
    RuntimeEndpoint("review_queue", "/debug/review-queue"),
)

RequestFn = Callable[[str, float], RuntimeHttpResponse]


def build_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def http_get(url: str, timeout_seconds: float = 5.0) -> RuntimeHttpResponse:
    started = time.monotonic()
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            return RuntimeHttpResponse(
                status_code=response.status,
                body=body,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return RuntimeHttpResponse(
            status_code=exc.code,
            body=body,
            elapsed_seconds=round(time.monotonic() - started, 3),
            error=f"HTTP {exc.code}",
        )
    except (TimeoutError, URLError, OSError) as exc:
        return RuntimeHttpResponse(
            status_code=None,
            body="",
            elapsed_seconds=round(time.monotonic() - started, 3),
            error=str(exc),
        )


def wait_for_readiness(
    url: str,
    *,
    timeout_seconds: float,
    interval_seconds: float = 1.0,
    request_fn: RequestFn = http_get,
) -> RuntimeHttpResponse:
    deadline = time.monotonic() + timeout_seconds
    last_response = RuntimeHttpResponse(None, "", 0.0, "readiness probe did not run")

    while time.monotonic() <= deadline:
        last_response = request_fn(url, interval_seconds)
        if last_response.status_code == 200 and last_response.error is None:
            return last_response
        time.sleep(interval_seconds)

    return RuntimeHttpResponse(
        status_code=last_response.status_code,
        body=last_response.body,
        elapsed_seconds=last_response.elapsed_seconds,
        error=last_response.error or f"readiness timed out after {timeout_seconds} seconds",
    )


def check_endpoint(
    base_url: str,
    endpoint: RuntimeEndpoint,
    *,
    request_fn: RequestFn = http_get,
    timeout_seconds: float = 5.0,
) -> RuntimeCheckResult:
    url = build_url(base_url, endpoint.path)
    return RuntimeCheckResult(endpoint=endpoint, url=url, response=request_fn(url, timeout_seconds))


def write_response_artifact(result: RuntimeCheckResult, artifact_dir: Path) -> Path:
    responses_dir = artifact_dir / "http-responses"
    responses_dir.mkdir(parents=True, exist_ok=True)
    path = responses_dir / f"{result.endpoint.name}.json"
    path.write_text(
        json.dumps(
            {
                "endpoint": asdict(result.endpoint),
                "url": result.url,
                "passed": result.passed,
                "response": asdict(result.response),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_failure_summary(results: Sequence[RuntimeCheckResult], artifact_dir: Path) -> Path:
    failures = [result for result in results if not result.passed]
    path = artifact_dir / "failure-summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Runtime Smoke Validation Summary", ""]
    if not failures:
        lines.append("All runtime smoke checks passed.")
    else:
        lines.append(f"{len(failures)} runtime smoke check(s) failed.")
        lines.append("")
        for failure in failures:
            lines.append(
                f"- {failure.endpoint.name}: expected HTTP {failure.endpoint.expected_status}, "
                f"got {failure.response.status_code}; error={failure.response.error!r}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def graceful_shutdown(process: subprocess.Popen[object], *, timeout_seconds: float = 10.0) -> bool:
    if process.poll() is not None:
        return True

    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_seconds)
        return False
    return True


def run_smoke_validation(
    *,
    base_url: str,
    artifact_dir: Path,
    readiness_timeout_seconds: float,
    endpoints: Sequence[RuntimeEndpoint] = DEFAULT_SMOKE_ENDPOINTS,
    request_fn: RequestFn = http_get,
) -> int:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    readiness_url = build_url(base_url, "/health")
    readiness = wait_for_readiness(
        readiness_url,
        timeout_seconds=readiness_timeout_seconds,
        request_fn=request_fn,
    )
    (artifact_dir / "startup-readiness.json").write_text(
        json.dumps(asdict(readiness), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if readiness.status_code != 200 or readiness.error is not None:
        write_failure_summary(
            [
                RuntimeCheckResult(
                    endpoint=RuntimeEndpoint("startup_readiness", "/health"),
                    url=readiness_url,
                    response=readiness,
                )
            ],
            artifact_dir,
        )
        return 1

    results = [check_endpoint(base_url, endpoint, request_fn=request_fn) for endpoint in endpoints]
    for result in results:
        write_response_artifact(result, artifact_dir)
    write_failure_summary(results, artifact_dir)
    return 0 if all(result.passed for result in results) else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local HTTP smoke validation against the orchestrator.")
    parser.add_argument("--base-url", default=os.getenv("RUNTIME_VALIDATION_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--artifact-dir", default=os.getenv("RUNTIME_VALIDATION_ARTIFACT_DIR", "runtime-validation-artifacts"))
    parser.add_argument("--readiness-timeout", type=float, default=60.0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return run_smoke_validation(
        base_url=args.base_url,
        artifact_dir=Path(args.artifact_dir),
        readiness_timeout_seconds=args.readiness_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
