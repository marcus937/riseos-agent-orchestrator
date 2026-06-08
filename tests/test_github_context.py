import asyncio
from typing import Any

from app.github_context import hydrate_github_context
from app.github_events import parse_github_event
from app.review_queue import process_review_work_item, review_work_item_from_parsed


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeGitHubClient:
    def __init__(
        self,
        *,
        response: dict[str, Any] | None = None,
        comments: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        comments_error: Exception | None = None,
    ) -> None:
        self.response = response or {}
        self.comments = comments or []
        self.error = error
        self.comments_error = comments_error
        self.fetch_commit_calls: list[tuple[str, str]] = []
        self.compare_branch_calls: list[tuple[str, str, str]] = []
        self.list_issue_comments_calls: list[tuple[str, int]] = []
        self.post_issue_comment_calls = 0
        self.apply_label_calls = 0

    async def fetch_commit(self, repo_full_name: str, commit_sha: str) -> dict[str, Any]:
        self.fetch_commit_calls.append((repo_full_name, commit_sha))
        if self.error:
            raise self.error
        return self.response

    async def compare_branch(self, repo_full_name: str, base: str, head: str) -> dict[str, Any]:
        self.compare_branch_calls.append((repo_full_name, base, head))
        if self.error:
            raise self.error
        return self.response

    async def list_issue_comments(self, repo_full_name: str, issue_number: int) -> list[dict[str, Any]]:
        self.list_issue_comments_calls.append((repo_full_name, issue_number))
        if self.comments_error:
            raise self.comments_error
        return self.comments

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> None:
        self.post_issue_comment_calls += 1
        raise AssertionError("hydration must not post GitHub comments")

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> None:
        self.apply_label_calls += 1
        raise AssertionError("hydration must not apply GitHub labels")


def test_hydration_disabled_preserves_existing_response_defaults() -> None:
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
        },
    )
    item = review_work_item_from_parsed(parsed)

    response = process_review_work_item(item)

    assert response.dry_run is True
    assert response.changed_files == []
    assert response.diff_summary is None
    assert response.github_context_available is False
    assert response.github_context_error is None
    assert response.runtime_evidence_context == []
    assert response.runtime_evidence_error is None
    assert response.runtime_evidence_truncated is False
    assert response.decision.decision == "APPROVED_FOR_HUMAN_REVIEW"


def test_valid_commit_hydration_adds_changed_files() -> None:
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
        },
    )
    item = review_work_item_from_parsed(parsed)
    client = FakeGitHubClient(
        response={
            "files": [
                {
                    "filename": "app/main.py",
                    "status": "modified",
                    "additions": 4,
                    "deletions": 1,
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                },
                {"filename": "tests/test_main.py", "status": "added", "additions": 12, "deletions": 0},
            ]
        }
    )

    context = run(hydrate_github_context(item, client))
    response = process_review_work_item(
        item,
        changed_files=context.changed_files,
        diff_summary=context.diff_summary,
        diff_patches=context.diff_patches,
        patch_truncated=context.patch_truncated,
        github_context_available=context.github_context_available,
        github_context_error=context.github_context_error,
        runtime_evidence_context=context.runtime_evidence_context,
        runtime_evidence_error=context.runtime_evidence_error,
        runtime_evidence_truncated=context.runtime_evidence_truncated,
    )

    assert client.fetch_commit_calls == [("riseos/example", "abc123")]
    assert client.compare_branch_calls == []
    assert client.list_issue_comments_calls == []
    assert response.github_context_available is True
    assert response.github_context_error is None
    assert response.changed_files == ["app/main.py", "tests/test_main.py"]
    assert response.diff_summary == "commit abc123: 2 changed file(s), +16/-1."
    assert response.diff_patches == [
        {
            "filename": "app/main.py",
            "status": "modified",
            "additions": 4,
            "deletions": 1,
            "patch": "@@ -1 +1 @@\n-old\n+new",
        }
    ]
    assert response.patch_truncated is False


def test_valid_pr_hydration_compares_base_and_head() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "feature/task", "sha": "def456"}},
        },
    )
    item = review_work_item_from_parsed(parsed)
    client = FakeGitHubClient(
        response={
            "files": [
                {
                    "filename": "README.md",
                    "status": "modified",
                    "additions": 2,
                    "deletions": 0,
                    "patch": "@@ -1 +1 @@\n-old docs\n+new docs",
                }
            ]
        }
    )

    context = run(hydrate_github_context(item, client, base_branch="main"))

    assert client.compare_branch_calls == [("riseos/example", "main", "feature/task")]
    assert client.fetch_commit_calls == []
    assert client.list_issue_comments_calls == [("riseos/example", 7)]
    assert context.github_context_available is True
    assert context.changed_files == ["README.md"]
    assert context.diff_patches[0]["patch"] == "@@ -1 +1 @@\n-old docs\n+new docs"


def test_pr_hydration_attaches_hermes_evidence_comments() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "agent-integration", "sha": "def456"}},
        },
    )
    item = review_work_item_from_parsed(parsed)
    client = FakeGitHubClient(
        response={"files": [{"filename": "app/main.py", "status": "modified", "additions": 1, "deletions": 0}]},
        comments=[
            {"id": 1, "body": "Ordinary review comment", "user": {"login": "marcus937"}},
            {
                "id": 2,
                "body": "## Hermes Runtime Validation\n\nStatus: PASSED\n\n### Evidence Packet\n- Manifest fetched: True\n- Bundle fetched: True",
                "created_at": "2026-06-08T00:00:00Z",
                "html_url": "https://github.com/riseos/example/pull/7#issuecomment-2",
                "user": {"login": "github-actions[bot]"},
            },
        ],
    )

    context = run(hydrate_github_context(item, client, base_branch="main"))
    response = process_review_work_item(
        item,
        changed_files=context.changed_files,
        diff_summary=context.diff_summary,
        runtime_evidence_context=context.runtime_evidence_context,
        runtime_evidence_error=context.runtime_evidence_error,
        runtime_evidence_truncated=context.runtime_evidence_truncated,
    )

    assert client.list_issue_comments_calls == [("riseos/example", 7)]
    assert context.runtime_evidence_error is None
    assert context.runtime_evidence_truncated is False
    assert response.runtime_evidence_context == [
        {
            "source": "github_issue_comment",
            "comment_id": 2,
            "author": "github-actions[bot]",
            "created_at": "2026-06-08T00:00:00Z",
            "html_url": "https://github.com/riseos/example/pull/7#issuecomment-2",
            "summary": "## Hermes Runtime Validation\n\nStatus: PASSED\n\n### Evidence Packet\n- Manifest fetched: True\n- Bundle fetched: True",
        }
    ]


def test_pr_hydration_records_runtime_evidence_comment_errors() -> None:
    parsed = parse_github_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "riseos/example"},
            "pull_request": {"number": 7, "head": {"ref": "agent-integration", "sha": "def456"}},
        },
    )
    item = review_work_item_from_parsed(parsed)
    client = FakeGitHubClient(
        response={"files": [{"filename": "app/main.py"}]},
        comments_error=RuntimeError("comments unavailable"),
    )

    context = run(hydrate_github_context(item, client, base_branch="main"))

    assert context.github_context_available is True
    assert context.runtime_evidence_context == []
    assert context.runtime_evidence_error == "comments unavailable"


def test_patch_truncation_limits_files_and_chars() -> None:
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
        },
    )
    item = review_work_item_from_parsed(parsed)
    files = [
        {
            "filename": f"file_{index}.py",
            "status": "modified",
            "additions": 1,
            "deletions": 1,
            "patch": "+" + ("x" * 9_000),
        }
        for index in range(25)
    ]
    client = FakeGitHubClient(response={"files": files})

    context = run(hydrate_github_context(item, client))

    assert len(context.diff_patches) == 5
    assert all(len(patch["patch"]) <= 8_000 for patch in context.diff_patches)
    assert sum(len(patch["patch"]) for patch in context.diff_patches) == 40_000
    assert context.patch_truncated is True


def test_github_error_is_captured_as_context_error() -> None:
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "missing",
        },
    )
    item = review_work_item_from_parsed(parsed)
    client = FakeGitHubClient(error=RuntimeError("GitHub GET commit failed with 404: Not Found"))

    context = run(hydrate_github_context(item, client))

    assert context.github_context_available is False
    assert "Not Found" in context.github_context_error


def test_missing_token_blocks_hydration_cleanly() -> None:
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
        },
    )
    item = review_work_item_from_parsed(parsed)
    client = FakeGitHubClient(error=RuntimeError("GITHUB_TOKEN is required for GitHub API requests."))

    context = run(hydrate_github_context(item, client))

    assert context.github_context_available is False
    assert "GITHUB_TOKEN" in context.github_context_error


def test_hydration_does_not_call_github_writes() -> None:
    parsed = parse_github_event(
        "push",
        {
            "repository": {"full_name": "riseos/example"},
            "ref": "refs/heads/agent-integration",
            "after": "abc123",
        },
    )
    item = review_work_item_from_parsed(parsed)
    client = FakeGitHubClient(response={"files": [{"filename": "app/main.py"}]})

    run(hydrate_github_context(item, client))

    assert client.post_issue_comment_calls == 0
    assert client.apply_label_calls == 0
