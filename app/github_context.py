from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.review_queue import ReviewWorkItem

MAX_PATCH_FILES = 20
MAX_PATCH_CHARS_PER_FILE = 8_000
MAX_PATCH_CHARS_TOTAL = 40_000
MAX_HERMES_EVIDENCE_COMMENTS = 5
MAX_HERMES_EVIDENCE_CHARS_PER_COMMENT = 4_000
MAX_HERMES_EVIDENCE_CHARS_TOTAL = 12_000
HERMES_EVIDENCE_MARKERS = (
    "## Hermes Runtime Validation",
    "### Evidence Packet",
    "### Evidence",
)


class GitHubContextClient(Protocol):
    async def fetch_commit(self, repo_full_name: str, commit_sha: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...

    async def compare_branch(self, repo_full_name: str, base: str, head: str) -> dict[str, Any] | list[dict[str, Any]]:
        ...


class GitHubContextResult(BaseModel):
    changed_files: list[str] = Field(default_factory=list)
    diff_summary: str | None = None
    diff_patches: list[dict[str, Any]] = Field(default_factory=list)
    patch_truncated: bool = False
    github_context_available: bool = False
    github_context_error: str | None = None
    runtime_evidence_context: list[dict[str, Any]] = Field(default_factory=list)
    runtime_evidence_error: str | None = None
    runtime_evidence_truncated: bool = False


async def hydrate_github_context(
    item: ReviewWorkItem,
    client: GitHubContextClient,
    *,
    base_branch: str = "main",
) -> GitHubContextResult:
    if not item.repo_full_name:
        return GitHubContextResult(github_context_error="repo_full_name is required for GitHub context hydration.")

    try:
        if item.pr_number is not None and item.branch:
            payload = await client.compare_branch(item.repo_full_name, base_branch, item.branch)
            result = _context_from_payload(payload, source=f"compare {base_branch}...{item.branch}")
            await _attach_runtime_evidence_context(item, client, result)
            return result

        if item.commit_sha:
            payload = await client.fetch_commit(item.repo_full_name, item.commit_sha)
            result = _context_from_payload(payload, source=f"commit {item.commit_sha}")
            await _attach_runtime_evidence_context(item, client, result)
            return result
    except Exception as exc:
        return GitHubContextResult(github_context_error=str(exc))

    result = GitHubContextResult(
        github_context_error="Not enough commit or pull request context to hydrate GitHub data."
    )
    await _attach_runtime_evidence_context(item, client, result)
    return result


def _context_from_payload(payload: dict[str, Any] | list[dict[str, Any]], *, source: str) -> GitHubContextResult:
    if not isinstance(payload, dict):
        return GitHubContextResult(github_context_error=f"Unexpected GitHub response for {source}.")

    files = payload.get("files")
    if not isinstance(files, list):
        return GitHubContextResult(
            diff_summary=f"GitHub context loaded for {source}, but no changed files were reported.",
            github_context_available=True,
        )

    changed_files = [
        str(file_info.get("filename"))
        for file_info in files
        if isinstance(file_info, dict) and file_info.get("filename")
    ]
    additions = sum(_int_field(file_info, "additions") for file_info in files if isinstance(file_info, dict))
    deletions = sum(_int_field(file_info, "deletions") for file_info in files if isinstance(file_info, dict))
    diff_patches, patch_truncated = _diff_patches_from_files(files)
    summary = f"{source}: {len(changed_files)} changed file(s), +{additions}/-{deletions}."
    if not diff_patches:
        summary = f"{summary} Patch content unavailable; only summary context is available."
    elif patch_truncated:
        summary = f"{summary} Patch content truncated."

    return GitHubContextResult(
        changed_files=changed_files,
        diff_summary=summary,
        diff_patches=diff_patches,
        patch_truncated=patch_truncated,
        github_context_available=True,
    )


async def _attach_runtime_evidence_context(
    item: ReviewWorkItem,
    client: GitHubContextClient,
    result: GitHubContextResult,
) -> None:
    target_number = item.pr_number or item.issue_number
    if not item.repo_full_name or target_number is None:
        return

    list_comments = getattr(client, "list_issue_comments", None)
    if list_comments is None:
        return

    try:
        comments = await list_comments(item.repo_full_name, target_number)
    except Exception as exc:
        result.runtime_evidence_error = str(exc)
        return

    evidence, truncated = _runtime_evidence_context_from_comments(comments)
    result.runtime_evidence_context = evidence
    result.runtime_evidence_truncated = truncated


def _runtime_evidence_context_from_comments(comments: Any) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(comments, list):
        return [], False

    evidence: list[dict[str, Any]] = []
    total_chars = 0
    truncated = False
    for comment in reversed(comments):
        if len(evidence) >= MAX_HERMES_EVIDENCE_COMMENTS:
            truncated = True
            break
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if not isinstance(body, str) or not _is_hermes_evidence_comment(body):
            continue

        remaining_total = MAX_HERMES_EVIDENCE_CHARS_TOTAL - total_chars
        if remaining_total <= 0:
            truncated = True
            break

        max_chars = min(MAX_HERMES_EVIDENCE_CHARS_PER_COMMENT, remaining_total)
        summary = body.strip()
        if len(summary) > max_chars:
            summary = summary[:max_chars]
            truncated = True

        total_chars += len(summary)
        evidence.append(
            {
                "source": "github_issue_comment",
                "comment_id": comment.get("id"),
                "author": _login_from_comment(comment),
                "created_at": comment.get("created_at"),
                "html_url": comment.get("html_url"),
                "summary": summary,
            }
        )

    if evidence and sum(1 for comment in comments if isinstance(comment, dict) and _is_hermes_evidence_comment(str(comment.get("body") or ""))) > len(evidence):
        truncated = True

    return list(reversed(evidence)), truncated


def _is_hermes_evidence_comment(body: str) -> bool:
    return any(marker in body for marker in HERMES_EVIDENCE_MARKERS)


def _login_from_comment(comment: dict[str, Any]) -> str | None:
    user = comment.get("user")
    if isinstance(user, dict) and user.get("login"):
        return str(user["login"])
    return None


def _int_field(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key, 0)
    return value if isinstance(value, int) else 0


def _diff_patches_from_files(files: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    diff_patches: list[dict[str, Any]] = []
    total_chars = 0
    truncated = len(files) > MAX_PATCH_FILES

    for file_info in files[:MAX_PATCH_FILES]:
        if not isinstance(file_info, dict):
            continue
        patch = file_info.get("patch")
        if not isinstance(patch, str):
            continue

        remaining_total = MAX_PATCH_CHARS_TOTAL - total_chars
        if remaining_total <= 0:
            truncated = True
            break

        max_chars = min(MAX_PATCH_CHARS_PER_FILE, remaining_total)
        if len(patch) > max_chars:
            patch = patch[:max_chars]
            truncated = True

        total_chars += len(patch)
        diff_patches.append(
            {
                "filename": str(file_info.get("filename") or ""),
                "status": str(file_info.get("status") or "unknown"),
                "additions": _int_field(file_info, "additions"),
                "deletions": _int_field(file_info, "deletions"),
                "patch": patch,
            }
        )

    if len(diff_patches) < sum(1 for file_info in files if isinstance(file_info, dict) and isinstance(file_info.get("patch"), str)):
        truncated = True

    return diff_patches, truncated
