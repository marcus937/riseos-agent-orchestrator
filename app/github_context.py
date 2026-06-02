from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.review_queue import ReviewWorkItem

MAX_PATCH_FILES = 20
MAX_PATCH_CHARS_PER_FILE = 8_000
MAX_PATCH_CHARS_TOTAL = 40_000


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
            return _context_from_payload(payload, source=f"compare {base_branch}...{item.branch}")

        if item.commit_sha:
            payload = await client.fetch_commit(item.repo_full_name, item.commit_sha)
            return _context_from_payload(payload, source=f"commit {item.commit_sha}")
    except Exception as exc:
        return GitHubContextResult(github_context_error=str(exc))

    return GitHubContextResult(
        github_context_error="Not enough commit or pull request context to hydrate GitHub data."
    )


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
