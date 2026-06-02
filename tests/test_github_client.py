import asyncio
import json
from typing import Any

import httpx

from app.clients.github import GitHubAPIError, GitHubClient, GitHubInputError, MissingGitHubTokenError


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def mock_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, base_url="https://api.github.test")


def test_fetch_commit_requests_commit_endpoint() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"sha": "abc123"})

    client = GitHubClient(token="token-123", api_base_url="https://api.github.test", http_client=mock_client(httpx.MockTransport(handler)))

    result = run(client.fetch_commit("riseos/example", "abc123"))

    assert result == {"sha": "abc123"}
    assert seen == {
        "method": "GET",
        "path": "/repos/riseos/example/commits/abc123",
        "auth": "Bearer token-123",
    }


def test_compare_branch_requests_compare_endpoint() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"status": "ahead"})

    client = GitHubClient(token="token-123", api_base_url="https://api.github.test", http_client=mock_client(httpx.MockTransport(handler)))

    result = run(client.compare_branch("riseos/example", "main", "agent-integration"))

    assert result == {"status": "ahead"}
    assert seen == {"method": "GET", "path": "/repos/riseos/example/compare/main...agent-integration"}


def test_post_issue_comment_posts_comment_body() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["json"] = request.read().decode("utf-8")
        return httpx.Response(201, json={"id": 42, "body": "review note"})

    client = GitHubClient(token="token-123", api_base_url="https://api.github.test", http_client=mock_client(httpx.MockTransport(handler)))

    result = run(client.post_issue_comment("riseos/example", 7, "review note"))

    assert result == {"id": 42, "body": "review note"}
    assert seen["method"] == "POST"
    assert seen["path"] == "/repos/riseos/example/issues/7/comments"
    assert json.loads(seen["json"]) == {"body": "review note"}


def test_apply_label_posts_labels_only() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["json"] = request.read().decode("utf-8")
        return httpx.Response(200, json=[{"name": "agent:review-needed"}])

    client = GitHubClient(token="token-123", api_base_url="https://api.github.test", http_client=mock_client(httpx.MockTransport(handler)))

    result = run(client.apply_label("riseos/example", 7, "agent:review-needed"))

    assert result == [{"name": "agent:review-needed"}]
    assert seen["method"] == "POST"
    assert seen["path"] == "/repos/riseos/example/issues/7/labels"
    assert json.loads(seen["json"]) == {"labels": ["agent:review-needed"]}


def test_missing_token_is_rejected_before_request() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = GitHubClient(token="", api_base_url="https://api.github.test", http_client=mock_client(httpx.MockTransport(handler)))

    try:
        run(client.fetch_commit("riseos/example", "abc123"))
    except MissingGitHubTokenError as exc:
        assert "GITHUB_TOKEN" in str(exc)
    else:
        raise AssertionError("MissingGitHubTokenError was not raised")

    assert called is False


def test_missing_inputs_are_rejected() -> None:
    client = GitHubClient(token="token-123", api_base_url="https://api.github.test", http_client=mock_client(httpx.MockTransport(lambda request: httpx.Response(200))))

    try:
        run(client.fetch_commit("", "abc123"))
    except GitHubInputError as exc:
        assert "repo_full_name" in str(exc)
    else:
        raise AssertionError("GitHubInputError was not raised")

    try:
        run(client.fetch_commit("riseos/example", ""))
    except GitHubInputError as exc:
        assert "commit_sha" in str(exc)
    else:
        raise AssertionError("GitHubInputError was not raised")

    try:
        run(client.apply_label("riseos/example", 0, "agent:ready"))
    except GitHubInputError as exc:
        assert "issue_number" in str(exc)
    else:
        raise AssertionError("GitHubInputError was not raised")


def test_non_2xx_response_raises_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = GitHubClient(token="token-123", api_base_url="https://api.github.test", http_client=mock_client(httpx.MockTransport(handler)))

    try:
        run(client.fetch_commit("riseos/example", "missing"))
    except GitHubAPIError as exc:
        assert exc.status_code == 404
        assert exc.detail == "Not Found"
        assert exc.path == "/repos/riseos/example/commits/missing"
    else:
        raise AssertionError("GitHubAPIError was not raised")
