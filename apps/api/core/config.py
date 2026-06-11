from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    org_id: str = "default"
    region: str = "us"
    admin_email: str = "admin@example.com"
    database_url: str = "postgresql+asyncpg://chronos:chronos@localhost:55432/chronos"
    # Connection pool sizing for the async engine. Defaults are tuned for a
    # multi-worker Fargate deployment against RDS; raise for higher concurrency.
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_recycle: int = 1800   # recycle connections every 30 min (RDS idle timeout)
    # asyncpg SSL mode for the DB connection. Empty disables explicit SSL (local
    # dev). Set to "require" for RDS, or "verify-full" with a CA bundle for strict
    # cert validation. Accepts any asyncpg sslmode string.
    db_ssl_mode: str = ""
    redis_url: str = "redis://localhost:6379"
    local_llm_base_url: str = "http://localhost:11434"
    local_llm_model: str = "llama3"
    backup_api_key: str = ""
    backup_model: str = "openrouter/minimax/minimax-m2.5:free"
    openrouter_api_key: str = ""
    openrouter_model: str = "openrouter/deepseek/deepseek-v4-flash:free"
    agent_model: str = "openrouter/deepseek/deepseek-v4-flash:free"
    openrouter_api_base: str = "https://openrouter.ai/api/v1"
    embedding_model: str = "google/gemini-embedding-2"
    embedding_dimensions: int = 1536
    fast_model: str = "openrouter/minimax/minimax-m2.5:free"
    vision_model: str = ""   # vision-capable model for OCR; empty disables OCR
    image_model: str = ""   # image generation model; empty disables image generation
    stt_model: str = ""    # speech-to-text model; empty disables STT
    tts_model: str = ""    # text-to-speech model; empty disables TTS
    local_llm_timeout_seconds: float = 2.0
    memory_retrieve_timeout_seconds: float = 1.5
    task_runner_max_concurrency: int = 4
    task_runner_max_attempts: int = 2
    task_runner_timeout_seconds: float = 1800.0
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
    frontend_base_url: str = "http://localhost:3000"
    # Connector / vault
    vault_encryption_key: str = ""   # 32-byte key as 64 hex chars; required outside dev
    composio_api_key: str = ""       # kept for backward compat; not actively used
    composio_callback_base_url: str = "http://localhost:8000"
    tavily_api_key: str = ""
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
    # Object storage: AWS S3 only.
    object_storage_backend: str = "s3"
    aws_s3_bucket: str = ""
    aws_s3_region: str = "us-east-1"
    aws_s3_endpoint: str = ""  # optional custom endpoint; blank uses AWS regional S3
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""

    @model_validator(mode="after")
    def validate_object_storage(self) -> "Settings":
        backend = self.object_storage_backend.lower()
        if backend != "s3":
            raise ValueError("OBJECT_STORAGE_BACKEND must be 's3'")
        if not self.aws_s3_bucket:
            raise ValueError("AWS_S3_BUCKET is required when OBJECT_STORAGE_BACKEND=s3")
        return self

    @property
    def object_storage_is_s3(self) -> bool:
        return True

    @property
    def object_storage_bucket(self) -> str:
        return self.aws_s3_bucket

    @property
    def object_storage_endpoint(self) -> str:
        return self.aws_s3_endpoint or f"s3.{self.aws_s3_region}.amazonaws.com"

    @property
    def object_storage_access_key(self) -> str:
        return self.aws_access_key_id

    @property
    def object_storage_secret_key(self) -> str:
        return self.aws_secret_access_key

    @property
    def object_storage_session_token(self) -> str:
        return self.aws_session_token

    @property
    def object_storage_secure(self) -> bool:
        return True

    @property
    def object_storage_region(self) -> str | None:
        return self.aws_s3_region

    @property
    def object_storage_bucket_location(self) -> str | None:
        return None if self.aws_s3_region == "us-east-1" else self.aws_s3_region

    @property
    def object_storage_health_name(self) -> str:
        return "s3"

    # Authorization (OpenFGA). Enforcement is OFF by default so the Phase-1 stub
    # behavior (allow-all) is preserved until an operator opts in. When
    # permissions_enforce is true AND openfga_api_url is set, permission.check
    # queries OpenFGA and raises PermissionDenied on a deny.
    permissions_enforce: bool = False
    openfga_api_url: str = ""          # e.g. http://localhost:8080 — empty disables
    openfga_store_id: str = ""         # resolved/created at bootstrap if empty
    openfga_model_id: str = ""         # resolved/written at bootstrap if empty

    # Sub-agent concurrency
    concurrent_sub_agents: int = 5

    # Agent-loop cognition (plan / reflect / dynamic routing). When disabled the
    # loop falls back to the proven model-native behavior. The planner/critic
    # also no-op automatically when no model API key is configured.
    agent_cognition_enabled: bool = True
    agent_max_reflections: int = 2
    agent_max_replans: int = 3

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
