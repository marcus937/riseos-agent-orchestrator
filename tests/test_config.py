from app.config import get_settings


def test_agent_bus_dispatch_settings_resolve_from_env(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("ENABLE_AGENT_BUS_DISPATCH", "true")
    monkeypatch.setenv("AGENT_BUS_BASE_URL", "https://agent-bus.riseconnect.us")
    monkeypatch.setenv("AGENT_BUS_TOKEN", "test-token")
    monkeypatch.setenv("AGENT_BUS_TIMEOUT_SECONDS", "30")

    settings = get_settings()

    assert settings.enable_agent_bus_dispatch is True
    assert settings.agent_bus_base_url == "https://agent-bus.riseconnect.us"
    assert settings.agent_bus_token == "test-token"
    assert settings.agent_bus_timeout_seconds == 30
    get_settings.cache_clear()
