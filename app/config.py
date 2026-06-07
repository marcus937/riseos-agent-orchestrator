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
    enable_bb_context_pack: bool = True
    bb_context_max_chars: int = 20000
    orchestrator_db_path: str | None = None
    orchestrator_admin_token: str | None = None
    orchestrator_max_review_items: int = 500
    review_claim_timeout_seconds: int = 900
    require_admin_token_for_debug_reads: bool = False
    enable_auto_review_processing: bool = False
    enable_github_context_hydration: bool = False
    enable_github_writeback: bool = False
    enable_task_dispatch: bool = False
    slack_webhook_url: str | None = None
    slack_bot_token: str | None = None
    slack_channel: str = "#jarvis-agent-orchestrator"
    orchestrator_slack_webhook_url: str | None = None
    orchestrator_slack_channel: str = "#jarvis-agent-orchestrator"
    hermes_slack_webhook_url: str | None = None
    hermes_slack_channel: str = "#jarvis-hermes-runtime"
    work_branch: str = "agent-integration"
    base_branch: str = "main"
    hermes_base_url: str | None = None
    hermes_token: str | None = None
    hermes_default_target: str = "https://example.com"
    hermes_enable_dispatch: bool = False
    hermes_m2_base_url: str | None = None
    hermes_m2_token: str | None = None
    hermes_m2_enable_dispatch: bool = False
    hermes_dgx_base_url: str | None = None
    hermes_dgx_token: str | None = None
    hermes_dgx_enable_dispatch: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    legacy_slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    legacy_slack_channel = os.getenv("SLACK_CHANNEL")
    orchestrator_slack_webhook_url = os.getenv("ORCHESTRATOR_SLACK_WEBHOOK_URL") or legacy_slack_webhook_url
    orchestrator_slack_channel = os.getenv("ORCHESTRATOR_SLACK_CHANNEL") or legacy_slack_channel or "#jarvis-agent-orchestrator"
    hermes_slack_webhook_url = os.getenv("HERMES_SLACK_WEBHOOK_URL") or orchestrator_slack_webhook_url
    hermes_slack_channel = os.getenv("HERMES_SLACK_CHANNEL") or os.getenv("ORCHESTRATOR_SLACK_CHANNEL") or legacy_slack_channel or "#jarvis-hermes-runtime"
    hermes_m2_base_url = os.getenv("HERMES_M2_BASE_URL") or os.getenv("HERMES_BASE_URL")
    hermes_m2_token = os.getenv("HERMES_M2_TOKEN") or os.getenv("HERMES_TOKEN")
    hermes_m2_enable_dispatch = _bool_env("HERMES_M2_ENABLE_DISPATCH") or _bool_env("HERMES_ENABLE_DISPATCH")
    return Settings(
        app_env=os.getenv("APP_ENV", "local"),
        github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET", ""),
        github_token=os.getenv("GITHUB_TOKEN"),
        github_app_id=os.getenv("GITHUB_APP_ID"),
        github_app_private_key_path=os.getenv("GITHUB_APP_PRIVATE_KEY_PATH"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_review_model=os.getenv("OPENAI_REVIEW_MODEL", "gpt-5.5-thinking"),
        enable_openai_review=_bool_env("ENABLE_OPENAI_REVIEW"),
        enable_bb_context_pack=os.getenv("ENABLE_BB_CONTEXT_PACK", "true").lower() == "true",
        bb_context_max_chars=_int_env("BB_CONTEXT_MAX_CHARS", 20000),
        orchestrator_db_path=os.getenv("ORCHESTRATOR_DB_PATH"),
        orchestrator_admin_token=os.getenv("ORCHESTRATOR_ADMIN_TOKEN"),
        orchestrator_max_review_items=_int_env("ORCHESTRATOR_MAX_REVIEW_ITEMS", 500),
        review_claim_timeout_seconds=_int_env("ORCHESTRATOR_REVIEW_CLAIM_TIMEOUT_SECONDS", 900),
        require_admin_token_for_debug_reads=_bool_env("REQUIRE_ADMIN_TOKEN_FOR_DEBUG_READS"),
        enable_auto_review_processing=_bool_env("ENABLE_AUTO_REVIEW_PROCESSING"),
        enable_github_context_hydration=_bool_env("ENABLE_GITHUB_CONTEXT_HYDRATION"),
        enable_github_writeback=_bool_env("ENABLE_GITHUB_WRITEBACK"),
        enable_task_dispatch=_bool_env("ENABLE_TASK_DISPATCH"),
        slack_webhook_url=hermes_slack_webhook_url or orchestrator_slack_webhook_url,
        slack_bot_token=os.getenv("SLACK_BOT_TOKEN"),
        slack_channel=orchestrator_slack_channel,
        orchestrator_slack_webhook_url=orchestrator_slack_webhook_url,
        orchestrator_slack_channel=orchestrator_slack_channel,
        hermes_slack_webhook_url=hermes_slack_webhook_url,
        hermes_slack_channel=hermes_slack_channel,
        work_branch=os.getenv("WORK_BRANCH", "agent-integration"),
        base_branch=os.getenv("BASE_BRANCH", "main"),
        hermes_base_url=hermes_m2_base_url,
        hermes_token=hermes_m2_token,
        hermes_default_target=os.getenv("HERMES_DEFAULT_TARGET", "https://example.com"),
        hermes_enable_dispatch=hermes_m2_enable_dispatch,
        hermes_m2_base_url=hermes_m2_base_url,
        hermes_m2_token=hermes_m2_token,
        hermes_m2_enable_dispatch=hermes_m2_enable_dispatch,
        hermes_dgx_base_url=os.getenv("HERMES_DGX_BASE_URL"),
        hermes_dgx_token=os.getenv("HERMES_DGX_TOKEN"),
        hermes_dgx_enable_dispatch=_bool_env("HERMES_DGX_ENABLE_DISPATCH"),
    )


def _bool_env(name: str) -> bool:
    return os.getenv(name, "").lower() == "true"


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default
