import asyncio
import json
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


class SecretArtifactHermesClient:
    def __init__(self) -> None:
        self.file_calls: list[tuple[str, str, str, str]] = []

    async def post_job(self, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"status": "PASSED", "jobId": "job-secret"}

    async def get_evidence_manifest(self, base_url: str, token: str, job_id: str) -> dict[str, Any]:
        return {
            "artifacts": [
                {"fileName": "page.json", "downloadUrl": "http://internal/files/page.json?token=secret-token"},
                {"fileName": "console.json"},
                {"fileName": "network.json"},
                {"fileName": "logs.json"},
                {"fileName": "summary.json"},
                {"fileName": "bad|name\nsecret-token.json", "retrievalNote": "unsafe|note\napi_key=abc123"},
            ]
        }

    async def get_evidence_bundle(self, base_url: str, token: str, job_id: str) -> dict[str, Any]:
        return {"content_type": "application/zip", "size": 0, "content": b""}

    async def get_evidence_file(self, base_url: str, token: str, job_id: str, file_name: str) -> dict[str, Any]:
        self.file_calls.append((base_url, token, job_id, file_name))
        payloads: dict[str, Any] = {
            "page.json": {
                "title": "Secret Dashboard password=hunter2",
                "finalUrl": "https://example.test/callback?token=secret-token&api_key=abc123",
                "httpStatus": 200,
                "userAgent": "Browser access_token=ua-secret",
            },
            "console.json": {
                "messages": [
                    {"level": "warning", "text": "console warning api_key=abc123"},
                    {"level": "error", "text": "console error password=hunter2"},
                ]
            },
            "network.json": {
                "requests": [
                    {
                        "url": "https://api.example.test/private?token=secret-token&api_key=abc123",
                        "status": 500,
                        "error": "Authorization: Bearer bearer-secret failed password=hunter2",
                    }
                ]
            },
            "logs.json": {"entries": [{"level": "error", "message": "log access_token=log-secret"}]},
            "summary.json": {"status": "passed"},
        }
        content = json.dumps(payloads[file_name]).encode("utf-8")
        return {"content_type": "application/json", "size": len(content), "content": content}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def settings() -> Settings:
    return Settings(
        enable_github_writeback=True,
        github_token="github-secret",
        hermes_m2_base_url="http://100.70.83.13:8787",
        hermes_m2_token="secret-token",
        hermes_m2_enable_dispatch=True,
        hermes_default_target="https://preview.vercel.app",
    )


def pr_payload() -> dict[str, Any]:
    return {
        "action": "labeled",
        "repository": {"full_name": "marcus937/riseos-agent-orchestrator"},
        "sender": {"login": "marcus"},
        "label": {"name": "playwright"},
        "pull_request": {
            "number": 92,
            "head": {"ref": "agent-integration", "sha": "abcdef1234567890", "repo": {"full_name": "marcus937/riseos-agent-orchestrator"}},
            "base": {"ref": "main", "repo": {"full_name": "marcus937/riseos-agent-orchestrator"}},
            "labels": [{"name": item} for item in ["runtime-agent", "playwright", "bb-review-needed"]],
        },
    }


def test_artifact_derived_comment_output_is_redacted_and_markdown_safe() -> None:
    parsed = parse_github_event("pull_request", pr_payload())
    github = FakeGitHubClient()
    hermes = SecretArtifactHermesClient()

    result = run(dispatch_hermes_runtime_validation(parsed, settings(), github_client=github, hermes_client=hermes, registry=InMemoryHermesDispatchRegistry()))

    comment = github.comments[0][2]
    assert result.evidence is not None
    assert {call[3] for call in hermes.file_calls} == {"summary.json", "page.json", "console.json", "network.json", "logs.json"}

    for leaked in [
        "secret-token",
        "abc123",
        "hunter2",
        "ua-secret",
        "bearer-secret",
        "log-secret",
        "http://internal/files/page.json",
        "unsafe|note",
    ]:
        assert leaked not in comment

    assert "[REDACTED]" in comment
    assert "Page title: Secret Dashboard password=[REDACTED]" in comment
    assert "Final URL: https://example.test/callback?token=[REDACTED]&api_key=[REDACTED]" in comment
    assert "User agent: Browser access_token=[REDACTED]" in comment
    assert "Console warning excerpts: console warning api_key=[REDACTED]" in comment
    assert "Console error excerpts: console error password=[REDACTED]; log access_token=[REDACTED]" in comment
    assert "Network non-2xx requests: https://api.example.test/private?token=[REDACTED]&api_key=[REDACTED]" in comment
    assert "Authorization: Bearer [REDACTED]" in comment
    assert "password=[REDACTED]" in comment
    assert "bad\\|name [REDACTED].json" in comment
    assert "bad|name" not in comment
    assert "GET /api/v1/evidence/job-secret/files/bad%7Cname%0A[REDACTED].json" in comment
