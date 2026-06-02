from typing import Any


class GitHubClient:
    """Placeholder wrapper for future GitHub App backed operations.

    MVP guardrail: write actions must be limited to comments and labels. No merge,
    branch mutation, or repository content writes belong in this client.
    """

    async def fetch_commit(self, repo_full_name: str, commit_sha: str) -> dict[str, Any]:
        raise NotImplementedError("GitHub commit fetch integration is not implemented yet.")

    async def compare_branch(self, repo_full_name: str, base: str, head: str) -> dict[str, Any]:
        raise NotImplementedError("GitHub branch comparison integration is not implemented yet.")

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        raise NotImplementedError("GitHub issue comment integration is not implemented yet.")

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> dict[str, Any]:
        raise NotImplementedError("GitHub label integration is not implemented yet.")
