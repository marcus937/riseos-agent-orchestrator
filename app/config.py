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
    openai_review_model: str = "gpt-5.5-thinking"
    enable_openai_review: bool = False
    orchestrator_db_path: str | None = None
    orchestrator_admin_token: str | None = None
    orchestrator_max_review_items: int = 500
    require_admin_token_for_debug_reads: bool = False
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
        openai_review_model=os.getenv("OPENAI_REVIEW_MODEL", "gpt-5.5-thinking"),
        enable_openai_review=os.getenv("ENABLE_OPENAI_REVIEW", "").lower() == "true",
        orchestrator_db_path=os.getenv("ORCHESTRATOR_DB_PATH"),
        orchestrator_admin_token=os.getenv("ORCHESTRATOR_ADMIN_TOKEN"),
        orchestrator_max_review_items=_int_env("ORCHESTRATOR_MAX_REVIEW_ITEMS", 500),
        require_admin_token_for_debug_reads=os.getenv("REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS", "").lower() == "true",
        enable_github_context_hydration=os.getenv("ENABLE_GITHUB_CONTEXT_HYDRATION", "").lower() == "true",
        enable_github_writeback=os.getenv("ENABLE_GITHUB_WRITEBACK", "").lower() == "true",
        work_branch=os.getenv("WORK_BRANCH", "agent-integration"),
        base_branch=os.getenv("BASE_BRANCH", "main"),
    )


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default
