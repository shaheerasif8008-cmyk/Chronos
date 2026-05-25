from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    org_id: str = "default"
    region: str = "us"
    admin_email: str = "admin@example.com"
    database_url: str = "postgresql+asyncpg://chronos:chronos@localhost:55432/chronos"
    redis_url: str = "redis://localhost:6379"
    local_llm_base_url: str = "http://localhost:11434"
    local_llm_model: str = "llama3"
    backup_api_key: str = ""
    backup_model: str = "openrouter/minimax/minimax-m2.5:free"
    openrouter_api_key: str = ""
    openrouter_model: str = "openrouter/deepseek/deepseek-v4-pro"
    agent_model: str = "openrouter/deepseek/deepseek-v4-pro"
    openrouter_api_base: str = "https://openrouter.ai/api/v1"
    embedding_model: str = "openrouter/nvidia/llama-nemotron-embed-vl-1b-v2:free"
    fast_model: str = "openrouter/minimax/minimax-m2.5:free"
    local_llm_timeout_seconds: float = 2.0
    memory_retrieve_timeout_seconds: float = 1.5
    jwt_secret: str = "change-me-in-dev"
    access_token_expire_minutes: int = 60

    # Auth: dev_otp (Phase 1 default), cognito, or both
    auth_provider: str = "dev_otp"
    cognito_region: str = "us-east-1"
    cognito_user_pool_id: str = ""
    cognito_app_client_id: str = ""
    cognito_app_client_secret: str = ""
    cognito_domain: str = ""
    cognito_callback_url: str = "http://localhost:3000/login/callback"
    cognito_auto_provision_members: bool = False
    # Connector / vault
    vault_encryption_key: str = ""   # 32-byte hex string; required outside dev
    composio_api_key: str = ""       # kept for backward compat; not actively used
    composio_callback_base_url: str = "http://localhost:8000"
    # Google OAuth2 — covers Gmail, Calendar, Drive
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/connectors/gmail/oauth-callback"
    # Other OAuth2 integrations — set CLIENT_ID + CLIENT_SECRET for each
    notion_client_id: str = ""
    notion_client_secret: str = ""
    slack_client_id: str = ""
    slack_client_secret: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    linear_client_id: str = ""
    linear_client_secret: str = ""
    hubspot_client_id: str = ""
    hubspot_client_secret: str = ""
    airtable_client_id: str = ""
    airtable_client_secret: str = ""
    jira_client_id: str = ""
    jira_client_secret: str = ""
    # Base URL for all OAuth callbacks (should be your public API URL)
    oauth_callback_base_url: str = "http://localhost:8000"
    demo_mode: bool = False
    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "chronos"
    minio_secret_key: str = "chronos123"
    minio_secure: bool = False
    minio_bucket: str = "chronos"

    # Sub-agent concurrency
    concurrent_sub_agents: int = 5

    # Context budgeting (category 7)
    max_context_tokens: int = 120_000   # conservative for frontier models
    response_reserve_tokens: int = 4_000

    # Per-org token budget guard (category 9) — 0 means unlimited
    per_org_daily_token_limit: int = 0

    # Observability (category 10)
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    sentry_dsn: str = ""

    model_config = SettingsConfigDict(env_file=(".env", "../../.env"), extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
