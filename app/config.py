import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "RiseOS Agent Orchestrator"
    app_env: str = "local"
    github_webhook_secret: str = ""
    github_token: str | None = None
    github_app_id: str | None = None
    github_app_private_key_path: str | None = None
    openai_api_key: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        app_env=os.getenv("APP_ENV", "local"),
        github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET", ""),
        github_token=os.getenv("GITHUB_TOKEN"),
        github_app_id=os.getenv("GITHUB_APP_ID"),
        github_app_private_key_path=os.getenv("GITHUB_APP_PRIVATE_KEY_PATH"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
    )
