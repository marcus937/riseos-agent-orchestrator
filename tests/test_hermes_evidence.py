import asyncio
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
        manifest_error: Exception | None = None,
        manifest_file: dict[str, Any] | None = None,
        manifest_file_error: Exception | None = None,
        bundle_error: Exception | None = None,
    ) -> None:
        self.response = response if response is not None else {"status": "PASSED", "jobId": "job-123"}
        self.manifest_error = manifest_error
        self.manifest_file = manifest_file
        self.manifest_file_error = manifest_file_error
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
        return self._manifest_payload()

    async def get_evidence_file(self, base_url: str, token: str, job_id: str, file_name: str) -> dict[str, Any]:
        self.file_calls.append((base_url, token, job_id, file_name))
        if self.manifest_file_error:
            raise self.manifest_file_error
        if file_name != "manifest.json" or self.manifest_file is None:
            raise RuntimeError(f"{file_name} unavailable")
        return self.manifest_file

    async def get_evidence_bundle(self, base_url: str, token: str, job_id: str) -> dict[str, Any]:
        self.bundle_calls.append((base_url, token, job_id))
        if self.bundle_error:
            raise self.bundle_error
        return {"content_type": "application/zip", "size": 4096}

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


def test_manifest_file_fallback_restores_evidence_when_manifest_endpoint_fails() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()
    hermes = FakeHermesEvidenceClient(
        manifest_error=RuntimeError("manifest endpoint unavailable"),
        manifest_file={
            "page": {"title": "Fallback Proof", "finalUrl": "https://preview.vercel.app/fallback", "httpStatus": 200},
            "artifacts": ["summary.json", "logs.json", "console.json", "network.json", "page.json", "screenshot.png"],
        },
    )

    result = run(dispatch_hermes_runtime_validation(parsed, settings(), github_client=github, hermes_client=hermes, registry=InMemoryHermesDispatchRegistry()))

    comment = github.comments[0][2]
    assert result.evidence is not None
    assert result.evidence.manifest_fetched is True
    assert result.evidence.bundle_fetched is True
    assert result.evidence.error is None
    assert hermes.manifest_calls == [("http://100.70.83.13:8787", "secret-token", "job-123")]
    assert hermes.file_calls == [("http://100.70.83.13:8787", "secret-token", "job-123", "manifest.json")]
    assert "Page title: Fallback Proof" in comment
    assert "Final URL: https://preview.vercel.app/fallback" in comment
    assert "| manifest unavailable" not in comment


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
