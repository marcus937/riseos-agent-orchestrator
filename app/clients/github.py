import os
from typing import Any

import httpx

GitHubResponse = dict[str, Any] | list[dict[str, Any]]


class GitHubClientError(Exception):
    """Base error for GitHub client failures."""


class MissingGitHubTokenError(GitHubClientError):
    """Raised when a GitHub token is required but not configured."""


class GitHubInputError(GitHubClientError):
    """Raised when required GitHub request inputs are missing."""


class GitHubAPIError(GitHubClientError):
    def __init__(self, method: str, path: str, status_code: int, detail: str) -> None:
        super().__init__(f"GitHub {method} {path} failed with {status_code}: {detail}")
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail


class GitHubClient:
    """Safe GitHub API wrapper for read/review operations.

    Write actions are intentionally limited to issue comments and labels. This
    client does not support merge, branch deletion, or repository file writes.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        api_base_url: str = "https://api.github.com",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token if token is not None else os.getenv("GITHUB_TOKEN", "")
        self._api_base_url = api_base_url.rstrip("/")
        self._http_client = http_client
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()

    async def fetch_commit(self, repo_full_name: str, commit_sha: str) -> GitHubResponse:
        self._require_value(repo_full_name, "repo_full_name")
        self._require_value(commit_sha, "commit_sha")
        return await self._request("GET", f"/repos/{repo_full_name}/commits/{commit_sha}")

    async def compare_branch(self, repo_full_name: str, base: str, head: str) -> GitHubResponse:
        self._require_value(repo_full_name, "repo_full_name")
        self._require_value(base, "base")
        self._require_value(head, "head")
        return await self._request("GET", f"/repos/{repo_full_name}/compare/{base}...{head}")

    async def list_open_issues(
        self,
        repo_full_name: str,
        *,
        labels: list[str] | None = None,
        sort: str = "created",
        direction: str = "asc",
    ) -> list[dict[str, Any]]:
        self._require_value(repo_full_name, "repo_full_name")
        payload = await self._request(
            "GET",
            f"/repos/{repo_full_name}/issues",
            params={
                "state": "open",
                "labels": ",".join(labels or []),
                "sort": sort,
                "direction": direction,
                "per_page": 100,
            },
        )
        if not isinstance(payload, list):
            raise GitHubAPIError("GET", f"/repos/{repo_full_name}/issues", 200, "Expected list response.")
        return payload

    async def post_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> GitHubResponse:
        self._require_value(repo_full_name, "repo_full_name")
        self._require_issue_number(issue_number)
        self._require_value(body, "body")
        return await self._request(
            "POST",
            f"/repos/{repo_full_name}/issues/{issue_number}/comments",
            json={"body": body},
        )

    async def apply_label(self, repo_full_name: str, issue_number: int, label: str) -> GitHubResponse:
        self._require_value(repo_full_name, "repo_full_name")
        self._require_issue_number(issue_number)
        self._require_value(label, "label")
        return await self._request(
            "POST",
            f"/repos/{repo_full_name}/issues/{issue_number}/labels",
            json={"labels": [label]},
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> GitHubResponse:
        if not self._token:
            raise MissingGitHubTokenError("GITHUB_TOKEN is required for GitHub API requests.")

        response = await self._client.request(
            method,
            f"{self._api_base_url}{path}",
            headers=self._headers(),
            **kwargs,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise GitHubAPIError(method, path, response.status_code, self._response_detail(response))

        if not response.content:
            return {}
        return response.json()

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=20.0)
        return self._http_client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def _require_value(value: str | None, field_name: str) -> None:
        if not value or not value.strip():
            raise GitHubInputError(f"{field_name} is required.")

    @staticmethod
    def _require_issue_number(issue_number: int | None) -> None:
        if not issue_number or issue_number < 1:
            raise GitHubInputError("issue_number must be a positive integer.")

    @staticmethod
    def _response_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"])
        return response.text
