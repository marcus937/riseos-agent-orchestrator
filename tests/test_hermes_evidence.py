import asyncio
import io
import json
import zipfile
from typing import Any

from app.config import Settings
from app.github_events import parse_github_event
from app.hermes_dispatch import InMemoryHermesDispatchRegistry, dispatch_hermes_runtime_validation


class FakeGitHubClient:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, str]] = []

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        self.comments.append((repo_full_name, issue_number, body))
        return {"id": 1}

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        self.labels.append((repo_full_name, issue_number, label))
        return {"labels": [label]}


class FakeHermesEvidenceClient:
    def __init__(
        self,
        *,
        response: dict[str, Any] | None = None,
        manifest_payload: dict[str, Any] | None = None,
        bundle_content: bytes | None = None,
        manifest_error: Exception | None = None,
        bundle_error: Exception | None = None,
    ) -> None:
        self.response = response if response is not None else {"status": "PASSED", "jobId": "job-123"}
        self.manifest_payload = manifest_payload
        self.bundle_content = bundle_content
        self.manifest_error = manifest_error
        self.bundle_error = bundle_error
        self.jobs: list[tuple[str, str, dict[str, Any]]] = []
        self.manifest_calls: list[tuple[str, str, str]] = []
        self.file_calls: list[tuple[str, str, str, str]] = []
        self.bundle_calls: list[tuple[str, str, str]] = []

    async def post_job(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.jobs.append((base_url, token, payload))
        return self.response

    async def get_evidence_manifest(self, base_url: str, token: str, job_id: str) -> dict[str, Any]:
        self.manifest_calls.append((base_url, token, job_id))
        if self.manifest_error:
            raise self.manifest_error
        return self.manifest_payload if self.manifest_payload is not None else self._manifest_payload()

    async def get_evidence_file(self, base_url: str, token: str, job_id: str, file_name: str) -> dict[str, Any]:
        self.file_calls.append((base_url, token, job_id, file_name))
        raise RuntimeError(f"{file_name} unavailable")

    async def get_evidence_bundle(self, base_url: str, token: str, job_id: str) -> dict[str, Any]:
        self.bundle_calls.append((base_url, token, job_id))
        if self.bundle_error:
            raise self.bundle_error
        bundle = {"content_type": "application/zip", "size": 4096}
        if self.bundle_content is not None:
            bundle["content"] = self.bundle_content
            bundle["size"] = len(self.bundle_content)
        return bundle

    def _manifest_payload(self) -> dict[str, Any]:
        return {
            "page": {"title": "Runtime Proof", "finalUrl": "https://preview.vercel.app/final", "httpStatus": 200},
            "console": {"warningCount": 2, "errorCount": 1},
            "network": {"failureCount": 3, "non2xxCount": 4},
            "artifacts": [
                {"fileName": "page.json", "contentType": "application/json", "size": 128, "sha256": "abc123"},
                {"fileName": "screenshot.png", "contentType": "image/png", "size": 2048, "sha256": "def456"},
            ],
        }


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def settings(**overrides: Any) -> Settings:
    base = {
        "enable_github_writeback": True,
        "hermes_m2_base_url": "http://100.70.83.13:8787",
        "hermes_m2_token": "secret-token",
        "hermes_m2_enable_dispatch": True,
        "hermes_default_target": "https://preview.vercel.app",
    }
    base.update(overrides)
    return Settings(**base)


def pr_payload() -> dict[str, Any]:
    return {
        "action": "labeled",
        "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
        "sender": {"login": "marcus"},
        "label": {"name": "playwright"},
        "pull_request": {
            "number": 51,
            "head": {"ref": "agent-integration", "sha": "abcdef1234567890", "repo": {"full_name": "marcus937/riseos-agent-orchestrator"}},
            "base": {"ref": "main", "repo": {"full_name": "marcus937/riseos-agent-orchestrator"}},
            "labels": [{"name": item} for item in ["runtime-agent", "playwright", "bb-review-needed"]],
        },
    }


def zip_bundle(files: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for file_name, payload in files.items():
            content = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            archive.writestr(file_name, content)
    return buffer.getvalue()


def test_successful_manifest_and_bundle_fetch_are_written_to_github_packet() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesEvidenceClient()

    result = run(dispatch_hermes_runtime_validation(parsed, settings(), github_client=github, hermes_client=hermes, registry=InMemoryHermesDispatchRegistry()))

    comment = github.comments[0][2]
    assert result.evidence is not None
    assert result.evidence.manifest_fetched is True
    assert result.evidence.bundle_fetched is True
    assert hermes.manifest_calls == [("http://100.70.83.13:8787", "secret-token", "job-123")]
    assert hermes.file_calls == []
    assert hermes.bundle_calls == [("http://100.70.83.13:8787", "secret-token", "job-123")]
    assert "Page title: Runtime Proof" in comment
    assert "Final URL: https://preview.vercel.app/final" in comment
    assert "HTTP status: 200" in comment
    assert "Screenshot presence: yes" in comment
    assert "Console warning count: 2" in comment
    assert "Network non-2xx count: 4" in comment
    assert "| screenshot.png | image/png | 2048 | def456 | GET /api/v1/evidence/job-123/files/screenshot.png |" in comment


def test_bundle_artifact_jsons_hydrate_unknown_manifest_metrics() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesEvidenceClient(
        manifest_payload={"artifacts": ["summary.json", "page.json", "console.json", "network.json", "screenshot.png"]},
        bundle_content=zip_bundle(
            {
                "artifacts/summary.json": {"status": "passed"},
                "artifacts/page.json": {
                    "title": "Jarvis Mission Control",
                    "finalUrl": "https://riseos-preview.vercel.app/dashboard",
                    "httpStatus": 200,
                },
                "artifacts/console.json": {
                    "messages": [
                        {"level": "warning", "text": "hydration warning"},
                        {"level": "error", "text": "hydration error"},
                    ]
                },
                "artifacts/network.json": {
                    "requests": [
                        {"url": "https://riseos-preview.vercel.app", "status": 200},
                        {"url": "https://api.test/fail", "status": 503},
                        {"url": "https://api.test/error", "error": "ECONNRESET"},
                    ]
                },
                "artifacts/screenshot.png": b"png-bytes",
            }
        ),
    )

    result = run(dispatch_hermes_runtime_validation(parsed, settings(), github_client=github, hermes_client=hermes, registry=InMemoryHermesDispatchRegistry()))

    comment = github.comments[0][2]
    assert result.evidence is not None
    assert result.evidence.page_title == "Jarvis Mission Control"
    assert result.evidence.final_url == "https://riseos-preview.vercel.app/dashboard"
    assert result.evidence.http_status == 200
    assert result.evidence.console_warning_count == 1
    assert result.evidence.console_error_count == 1
    assert result.evidence.network_failure_count == 1
    assert result.evidence.network_non_2xx_count == 1
    assert result.evidence.screenshot_present is True
    assert "Page title: Jarvis Mission Control" in comment
    assert "Final URL: https://riseos-preview.vercel.app/dashboard" in comment
    assert "HTTP status: 200" in comment
    assert "Console warning count: 1" in comment
    assert "Console error count: 1" in comment
    assert "Network failure count: 1" in comment
    assert "Network non-2xx count: 1" in comment
    assert "Screenshot presence: yes" in comment


def test_nested_job_id_fetches_canonical_manifest_and_bundle() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesEvidenceClient(response={"status": "PASSED", "job": {"id": "job-nested-123"}})

    result = run(dispatch_hermes_runtime_validation(parsed, settings(), github_client=github, hermes_client=hermes, registry=InMemoryHermesDispatchRegistry()))

    assert result.job_id == "job-nested-123"
    assert result.evidence is not None
    assert result.evidence.manifest_fetched is True
    assert hermes.manifest_calls == [("http://100.70.83.13:8787", "secret-token", "job-nested-123")]
    assert hermes.file_calls == []
    assert hermes.bundle_calls == [("http://100.70.83.13:8787", "secret-token", "job-nested-123")]
    assert "Job ID: job-nested-123" in github.comments[0][2]


def test_manifest_endpoint_failure_does_not_fall_back_to_manifest_file() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesEvidenceClient(manifest_error=RuntimeError("manifest endpoint unavailable"))

    result = run(dispatch_hermes_runtime_validation(parsed, settings(), github_client=github, hermes_client=hermes, registry=InMemoryHermesDispatchRegistry()))

    comment = github.comments[0][2]
    assert result.evidence is not None
    assert result.evidence.manifest_fetched is False
    assert result.evidence.bundle_fetched is True
    assert hermes.manifest_calls == [("http://100.70.83.13:8787", "secret-token", "job-123")]
    assert hermes.file_calls == []
    assert "Evidence manifest could not be fetched from Hermes." not in comment
    assert "manifest endpoint unavailable" in comment


def test_missing_job_id_keeps_existing_writeback_without_evidence_fetch() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesEvidenceClient(response={"status": "PASSED"})

    result = run(dispatch_hermes_runtime_validation(parsed, settings(), github_client=github, hermes_client=hermes, registry=InMemoryHermesDispatchRegistry()))

    assert result.job_id is None
    assert result.evidence is None
    assert hermes.manifest_calls == []
    assert hermes.file_calls == []
    assert hermes.bundle_calls == []
    assert "Job ID: not-created" in github.comments[0][2]


def test_manifest_failure_does_not_block_bundle_fetch_or_writeback() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesEvidenceClient(manifest_error=RuntimeError("token=secret-token manifest failed"))

    result = run(dispatch_hermes_runtime_validation(parsed, settings(), github_client=github, hermes_client=hermes, registry=InMemoryHermesDispatchRegistry()))

    assert result.status == "PASSED"
    assert result.evidence is not None
    assert result.evidence.manifest_fetched is False
    assert result.evidence.bundle_fetched is True
    assert "secret-token" not in github.comments[0][2]
    assert "[REDACTED]" in github.comments[0][2]


def test_bundle_failure_does_not_drop_manifest_metadata() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesEvidenceClient(bundle_error=RuntimeError("Authorization: Bearer secret-token bundle failed"))

    result = run(dispatch_hermes_runtime_validation(parsed, settings(), github_client=github, hermes_client=hermes, registry=InMemoryHermesDispatchRegistry()))

    comment = github.comments[0][2]
    assert result.evidence is not None
    assert result.evidence.manifest_fetched is True
    assert result.evidence.bundle_fetched is False
    assert "Page title: Runtime Proof" in comment
    assert "secret-token" not in comment
    assert "[REDACTED]" in comment
