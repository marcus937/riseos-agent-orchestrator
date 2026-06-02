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
    enable_openai_review: bool = False
    orchestrator_db_path: str | None = None
    enable_github_context_hydration: bool = False
    enable_github_writeback: bool = False
    work_branch: str = "agent-integration"
    base_branch: str = "main"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        app_env=os.getenv("APP_ENV", "local"),
        github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET", ""),
        github_token=os.getenv("GITHUB_TOKEN"),
        github_app_id=os.getenv("GITHUB_APP_ID"),
        github_app_private_key_path=os.getenv("GITHUB_APP_PRIVATE_KEY_PATH"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        enable_openai_review=os.getenv("ENABLE_OPENAI_REVIEW", "").lower() == "true",
        orchestrator_db_path=os.getenv("ORCHESTRATOR_DB_PATH"),
        enable_github_context_hydration=os.getenv("ENABLE_GITHUB_CONTEXT_HYDRATION", "").lower() == "true",
        enable_github_writeback=os.getenv("ENABLE_GITHUB_WRITEBACK", "").lower() == "true",
        work_branch=os.getenv("WORK_BRANCH", "agent-integration"),
        base_branch=os.getenv("BASE_BRANCH", "main"),
    )
