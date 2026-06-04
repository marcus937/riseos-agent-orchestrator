from app.clients.github import GitHubClient


def test_github_client_uses_api_base_url_env(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_API_BASE_URL", "http://127.0.0.1:9001/")

    client = GitHubClient(token="token")

    assert client._api_base_url == "http://127.0.0.1:9001"
