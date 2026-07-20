from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
import re
from urllib.parse import SplitResult, urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_DEFAULT_JWT_SECRET = "change-me-in-dev"
_INSECURE_VAULT_KEYS = {"", "0" * 64}
_COGNITO_DOMAIN_PREFIX_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I)


_API_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = (
    _API_ROOT.parents[1]
    if _API_ROOT.name == "api" and _API_ROOT.parent.name == "apps"
    else _API_ROOT
)
_ENV_FILE = _REPO_ROOT / ".env"


def _credential_https_origin(
    value: str,
    *,
    setting_name: str,
    allowed_paths: frozenset[str] = frozenset({"", "/"}),
) -> SplitResult:
    """Validate an HTTPS origin that will receive a production credential.

    These settings are origins, not arbitrary request URLs. Rejecting alternate
    authorities, ports, paths, queries, and fragments prevents a typo or URL
    smuggling from redirecting an OAuth code or provider key to an unexpected
    endpoint. A DNS name remains configurable so supported custom Cognito and
    self-hosted Langfuse deployments continue to work.
    """

    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        explicit_port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{setting_name} must be a valid HTTPS origin") from exc

    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or explicit_port is not None
        or parsed.path not in allowed_paths
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{setting_name} must be an HTTPS origin without credentials, port, path, query, or fragment"
        )

    if hostname.endswith(".") or len(hostname) > 253 or "." not in hostname:
        raise ValueError(f"{setting_name} must use a fully-qualified DNS hostname")
    try:
        ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError(f"{setting_name} must use a DNS hostname, not an IP address")
    if any(_DNS_LABEL_RE.fullmatch(label) is None for label in hostname.split(".")):
        raise ValueError(f"{setting_name} contains an invalid DNS hostname")
    return parsed


class Settings(BaseSettings):
    # Deployment environment. "development" relaxes secret/auth guards for local
    # work; anything else ("staging"/"production") is treated as production and
    # the startup guards below refuse to boot on insecure defaults.
    environment: str = "development"
    org_id: str = "default"
    region: str = "us"
    # Apex domain for per-tenant subdomains: novatech.<base_domain>.
    base_domain: str = "cognisiatech.com"
    admin_email: str = "admin@example.com"
    database_url: str = "postgresql+asyncpg://chronos:chronos@localhost:55432/chronos"
    # Connection pool sizing for the async engine. Defaults are tuned for a
    # multi-worker Fargate deployment against RDS; raise for higher concurrency.
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_recycle: int = 1800  # recycle connections every 30 min (RDS idle timeout)
    # asyncpg SSL mode for the DB connection. Empty disables explicit SSL (local
    # dev). Set to "require" for RDS, or "verify-full" with a CA bundle for strict
    # cert validation. Accepts any asyncpg sslmode string.
    db_ssl_mode: str = ""
    redis_url: str = "redis://localhost:6379"
    local_llm_base_url: str = "http://localhost:11434"
    local_llm_model: str = "llama3"
    backup_api_key: str = ""
    # Independent failover. Production requires a direct provider distinct from
    # OpenRouter so a routing-provider outage cannot disable both paths.
    backup_model: str = "anthropic/claude-sonnet-5"
    openrouter_api_key: str = ""
    # Fallback for the agent model — keep it a real (non-free) tier so a fallback
    # turn isn't noticeably weaker than the primary.
    openrouter_model: str = "openrouter/openai/gpt-5.4-nano"
    # Primary model for task execution / the agent loop. A strong reasoning model
    # by default so plans and tool use are reliable; the loop can still escalate to
    # the strongest tier (gpt-5.4-mini) when it gets stuck.
    agent_model: str = "openrouter/deepseek/deepseek-v4-pro"
    openrouter_api_base: str = "https://openrouter.ai/api/v1"
    embedding_model: str = "google/gemini-embedding-2"
    embedding_dimensions: int = 1536
    # Fast/cheap model for routing, intent, memory extraction, and planning helpers.
    # A real lightweight model (not a free tier) so these steps are dependable.
    fast_model: str = "openrouter/openai/gpt-5.4-nano"
    vision_model: str = "openrouter/openai/gpt-4o-mini"
    image_model: str = "openrouter/google/gemini-3.1-flash-image"
    stt_model: str = "openrouter/openai/gpt-4o-mini-transcribe"
    tts_model: str = "openrouter/x-ai/grok-voice-tts-1.0"
    local_llm_timeout_seconds: float = 2.0
    memory_retrieve_timeout_seconds: float = 1.5
    task_runner_max_concurrency: int = 4
    task_runner_max_attempts: int = 2
    task_runner_timeout_seconds: float = 1800.0
    # Durable runtime: distributed task leases + scheduler lock so multiple API
    # workers coordinate safely. A worker holds a lease while executing a task and
    # renews it on a heartbeat; if the worker dies the lease expires and the reaper
    # re-queues the task. The scheduler poll runs under a single-holder lock so a
    # due schedule fires exactly once across the fleet.
    task_lease_ttl_seconds: int = 120
    task_lease_heartbeat_seconds: int = 30
    task_reaper_interval_seconds: int = 60
    # Durable monitor poller. Definitions have their own bounded cadence and
    # retry state; these are platform ceilings, not tenant-editable shortcuts.
    monitor_poll_interval_seconds: int = 30
    monitor_min_interval_seconds: int = 60
    monitor_max_interval_seconds: int = 86_400
    monitor_max_per_org: int = 100
    monitor_max_runs_per_org_cycle: int = 10
    monitor_fetch_timeout_seconds: float = 15.0
    monitor_fetch_max_bytes: int = 1_048_576
    monitor_lease_seconds: int = 90
    # Rich artifact previews remain strictly bounded. ZIP-based Office files are
    # checked against the uncompressed ceiling before a parser sees them; native
    # PDF parsing/rasterization runs in a credential-scrubbed subprocess.
    artifact_preview_max_bytes: int = 26_214_400
    artifact_preview_max_uncompressed_bytes: int = 104_857_600
    artifact_preview_max_pdf_pages: int = 50
    # Public artifact links are bearer credentials and therefore always expire.
    # Admins can choose a shorter duration per link, bounded by this ceiling.
    artifact_share_ttl_hours: int = 168
    # Project bundles contain only artifacts explicitly shared to that project.
    project_export_max_bytes: int = 104_857_600
    project_export_max_artifacts: int = 500
    # Every user-controlled file ingress is streamed to a private ClamAV
    # daemon before it is persisted or handed to a browser. Production refuses
    # to start unless this fail-closed boundary is enabled. The daemon is an ECS
    # sidecar in production and an optional docker-compose service locally.
    malware_scan_required: bool = False
    clamav_host: str = "127.0.0.1"
    clamav_port: int = 3310
    clamav_timeout_seconds: float = 20.0
    clamav_max_bytes: int = 52_428_800
    clamav_max_signature_age_hours: int = 48

    @model_validator(mode="after")
    def validate_monitor_runtime(self) -> "Settings":
        if not 5 <= self.monitor_poll_interval_seconds <= 300:
            raise ValueError("MONITOR_POLL_INTERVAL_SECONDS must be between 5 and 300")
        if (
            not 60
            <= self.monitor_min_interval_seconds
            <= self.monitor_max_interval_seconds
            <= 604_800
        ):
            raise ValueError(
                "Monitor interval bounds must be between 60 and 604800 seconds"
            )
        if not 1 <= self.monitor_max_per_org <= 10_000:
            raise ValueError("MONITOR_MAX_PER_ORG must be between 1 and 10000")
        if not 1 <= self.monitor_max_runs_per_org_cycle <= 100:
            raise ValueError("MONITOR_MAX_RUNS_PER_ORG_CYCLE must be between 1 and 100")
        if not 1 <= self.monitor_fetch_timeout_seconds <= 60:
            raise ValueError("MONITOR_FETCH_TIMEOUT_SECONDS must be between 1 and 60")
        if not 65_536 <= self.monitor_fetch_max_bytes <= 5_242_880:
            raise ValueError(
                "MONITOR_FETCH_MAX_BYTES must be between 65536 and 5242880"
            )
        if self.monitor_lease_seconds <= self.monitor_fetch_timeout_seconds:
            raise ValueError(
                "MONITOR_LEASE_SECONDS must exceed MONITOR_FETCH_TIMEOUT_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def validate_artifact_limits(self) -> "Settings":
        if not 1_048_576 <= self.artifact_preview_max_bytes <= 104_857_600:
            raise ValueError(
                "ARTIFACT_PREVIEW_MAX_BYTES must be between 1048576 and 104857600"
            )
        if (
            not self.artifact_preview_max_bytes
            <= self.artifact_preview_max_uncompressed_bytes
            <= 536_870_912
        ):
            raise ValueError(
                "ARTIFACT_PREVIEW_MAX_UNCOMPRESSED_BYTES must be between the input limit and 536870912"
            )
        if not 1 <= self.artifact_preview_max_pdf_pages <= 250:
            raise ValueError("ARTIFACT_PREVIEW_MAX_PDF_PAGES must be between 1 and 250")
        if not 1 <= self.artifact_share_ttl_hours <= 720:
            raise ValueError("ARTIFACT_SHARE_TTL_HOURS must be between 1 and 720")
        if not 1_048_576 <= self.project_export_max_bytes <= 536_870_912:
            raise ValueError(
                "PROJECT_EXPORT_MAX_BYTES must be between 1048576 and 536870912"
            )
        if not 1 <= self.project_export_max_artifacts <= 2_000:
            raise ValueError("PROJECT_EXPORT_MAX_ARTIFACTS must be between 1 and 2000")
        return self

    @model_validator(mode="after")
    def validate_file_security(self) -> "Settings":
        if not self.clamav_host.strip():
            raise ValueError("CLAMAV_HOST must not be empty")
        if not 1 <= self.clamav_port <= 65_535:
            raise ValueError("CLAMAV_PORT must be between 1 and 65535")
        if not 1 <= self.clamav_timeout_seconds <= 120:
            raise ValueError("CLAMAV_TIMEOUT_SECONDS must be between 1 and 120")
        if not 25 * 1024 * 1024 <= self.clamav_max_bytes <= 100 * 1024 * 1024:
            raise ValueError("CLAMAV_MAX_BYTES must be between 25 MiB and 100 MiB")
        if not 1 <= self.clamav_max_signature_age_hours <= 168:
            raise ValueError("CLAMAV_MAX_SIGNATURE_AGE_HOURS must be between 1 and 168")
        return self

    @model_validator(mode="after")
    def validate_egress_allowlists(self) -> "Settings":
        from core.egress_policy import parse_egress_allowlist

        parse_egress_allowlist(self.e2b_computer_egress_allowlist)
        parse_egress_allowlist(self.e2b_repo_egress_allowlist)
        return self

    runtime_auto_install_tools: bool = True
    runtime_tool_install_timeout_seconds: float = 180.0
    e2b_api_key: str = ""
    # Optional custom E2B template with the dependencies Chronos tools need
    # (notably pandas/matplotlib/numpy for data.run). The base E2B template is
    # used when blank.
    e2b_template_id: str = ""
    # Deprecated compatibility flag. Code, data, and bundled skill execution
    # are always deny-egress; this value is deliberately ignored by their
    # runtime factory.
    e2b_allow_internet_access: bool = False
    # Cloud-computer sessions are a separate profile because web/package tasks
    # need egress. Production deployment config should always set this value
    # explicitly even though the product default is web-capable.
    e2b_computer_allow_internet_access: bool = True
    # Comma-separated organization ceiling. A computer session must request a
    # human-approved exact-domain subset of this list before E2B egress opens.
    e2b_computer_egress_allowlist: str = ""
    # Cloud computers must use a dedicated E2B desktop template. Reusing the
    # code/data template would silently turn GUI controls into a headless stub.
    e2b_computer_template_id: str = ""
    e2b_computer_idle_timeout_seconds: int = 900
    e2b_computer_max_session_seconds: int = 14_400
    e2b_computer_max_active_per_member: int = 2
    e2b_computer_max_active_per_org: int = 20
    e2b_computer_screen_width: int = 1280
    e2b_computer_screen_height: int = 800
    e2b_sandbox_timeout_seconds: int = 1800
    # Coding workspaces are long-lived E2B sandboxes with Postgres metadata and
    # S3 snapshots. Keep this profile separate from ephemeral code/data runs so
    # enabling Git/package egress cannot weaken those deny-egress sandboxes.
    e2b_repo_enabled: bool = False
    e2b_repo_template_id: str = ""
    e2b_repo_allow_internet_access: bool = False
    # Exact domains (or leading *. suffix rules) required by Git/package work.
    # E2B treats allow_out as deny-by-default and gives allowed entries
    # precedence over the explicit IPv4/IPv6 deny-all rules.
    e2b_repo_egress_allowlist: str = ""
    e2b_repo_timeout_seconds: int = 21_600
    e2b_repo_command_timeout_seconds: int = 300
    e2b_repo_max_snapshot_bytes: int = 52_428_800
    e2b_repo_max_workspaces_per_org: int = 50
    e2b_repo_max_workspaces_per_task: int = 3
    jwt_secret: str = "change-me-in-dev"
    access_token_expire_minutes: int = 60

    # Reject legacy org-less session tokens. Enforced by default, but only in
    # production (dev/test keep org-less tokens for ergonomics — every minted
    # token already carries `org`). During a rollout, set the grace window below
    # so active sessions drain instead of breaking the moment enforcement lands.
    enforce_org_bound_tokens: bool = True
    # ISO-8601 timestamp. While now < this, legacy org-less tokens are still
    # accepted in production (with a warning). Empty = no grace (immediate). Set
    # to roughly one access-token lifetime past a deploy that flips enforcement.
    org_bound_tokens_grace_until: str = ""

    # Auth: dev_otp (Phase 1 default), cognito, or both
    auth_provider: str = "dev_otp"
    cognito_region: str = "us-east-1"
    cognito_user_pool_id: str = ""
    cognito_app_client_id: str = ""
    cognito_app_client_secret: str = ""
    cognito_domain: str = ""
    # Optional token-validation overrides for a Cognito custom-domain or
    # multi-region front door. Hosted UI and token issuer are distinct concepts:
    # leave blank for the standard regional user-pool issuer/JWKS.
    cognito_issuer_url: str = ""
    cognito_jwks_url: str = ""
    cognito_callback_url: str = "http://localhost:3000/login/callback"
    cognito_auto_provision_members: bool = False
    frontend_base_url: str = "http://localhost:3000"
    # Public client-facing destinations. Chronos never invents legal copy;
    # production points at owner-approved, published documents.
    terms_url: str = ""
    privacy_url: str = ""
    support_url: str = ""
    status_url: str = ""
    # Connector / vault
    vault_encryption_key: str = ""  # 32-byte key as 64 hex chars; required outside dev
    composio_api_key: str = (
        ""  # set to route all SaaS connectors through Composio managed auth
    )
    composio_entity_scope: str = (
        "member"  # "member" (per-user connections) or "org" (one shared connection)
    )
    composio_callback_base_url: str = "http://localhost:8000"
    tavily_api_key: str = ""
    browserbase_api_key: str = ""
    # The browser *operator* is separate from browser.search. Production uses
    # Browserbase Contexts for encrypted, restart-safe login state and remote
    # sessions that any API replica can reconnect to. Local Chromium remains a
    # development-only fallback when this flag is false.
    browserbase_operator_enabled: bool = False
    browserbase_project_id: str = ""
    browserbase_region: str = "us-east-1"
    browserbase_session_timeout_seconds: int = 14_400
    browserbase_search_url: str = "https://api.browserbase.com/v1/search"
    # Google OAuth2 — covers Gmail, Calendar, Drive
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/connectors/gmail/oauth-callback"
    # Other OAuth2 integrations — set CLIENT_ID + CLIENT_SECRET for each
    notion_client_id: str = ""
    notion_client_secret: str = ""
    slack_client_id: str = ""
    slack_client_secret: str = ""
    # Public agent ingress. Empty values leave the corresponding publication
    # type visibly degraded; they are never replaced with a shared dev secret.
    slack_signing_secret: str = ""
    teams_bot_app_id: str = ""
    teams_bot_jwks_url: str = "https://login.botframework.com/v1/.well-known/keys"
    teams_bot_issuer: str = "https://api.botframework.com"
    sendgrid_inbound_public_key: str = ""
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
    # Comma-separated extra OIDC endpoint hosts permitted when an administrator
    # manually configures cross-host authorize/token/JWKS URLs. Same-host issuer
    # endpoints and endpoints returned by validated discovery need no entry.
    sso_endpoint_host_allowlist: str = ""
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
    def is_production(self) -> bool:
        return self.environment.strip().lower() not in {
            "development",
            "dev",
            "local",
            "test",
        }

    @model_validator(mode="after")
    def enforce_production_secrets(self) -> "Settings":
        """Fail closed on insecure defaults outside development.

        A deployment must never run with the shipped JWT secret, a missing/
        all-zeros vault key, or dev OTP enabled. These are the highest-impact
        misconfigurations (token forgery, credential disclosure, account
        takeover), so we refuse to boot rather than start in a vulnerable state.
        """
        if not self.is_production:
            return self
        problems: list[str] = []
        if self.jwt_secret == _DEFAULT_JWT_SECRET or len(self.jwt_secret) < 32:
            problems.append(
                "JWT_SECRET must be at least 32 characters and not the default"
            )
        if (
            self.vault_encryption_key in _INSECURE_VAULT_KEYS
            or re.fullmatch(r"[0-9a-fA-F]{64}", self.vault_encryption_key) is None
        ):
            problems.append(
                "VAULT_ENCRYPTION_KEY must be a real 32-byte (64 hex char) key"
            )
        if self.auth_provider in {"dev_otp", "both"}:
            problems.append("AUTH_PROVIDER must not enable dev_otp in production")
        if self.demo_mode:
            problems.append("DEMO_MODE must be false")
        if not self.permissions_enforce:
            problems.append("PERMISSIONS_ENFORCE must be true")
        if not self.openfga_api_url.strip():
            problems.append("OPENFGA_API_URL is required")
        if len(self.openfga_api_token.strip()) < 32:
            problems.append("OPENFGA_API_TOKEN must be a strong pre-shared key")
        if not self.enforce_org_bound_tokens:
            problems.append("ENFORCE_ORG_BOUND_TOKENS must be true")
        if self.per_org_daily_token_limit <= 0:
            problems.append("PER_ORG_DAILY_TOKEN_LIMIT must be greater than zero")
        if self.aws_s3_endpoint.strip():
            problems.append(
                "AWS_S3_ENDPOINT must be blank so production uses regional AWS S3"
            )
        if self.aws_access_key_id.strip() or self.aws_secret_access_key.strip():
            problems.append(
                "static AWS access keys are forbidden; use the task IAM role"
            )
        if self.db_ssl_mode.strip().lower() not in {
            "require",
            "verify-ca",
            "verify-full",
        }:
            problems.append("DB_SSL_MODE must require TLS")
        if not self.redis_url.strip().lower().startswith("rediss://"):
            problems.append("REDIS_URL must use rediss:// TLS")
        if self.auth_provider != "cognito":
            problems.append("AUTH_PROVIDER must be cognito")
        cognito_required = {
            "COGNITO_USER_POOL_ID": self.cognito_user_pool_id,
            "COGNITO_APP_CLIENT_ID": self.cognito_app_client_id,
            "COGNITO_DOMAIN": self.cognito_domain,
        }
        for name, value in cognito_required.items():
            if not value.strip():
                problems.append(f"{name} is required")
        cognito_domain = self.cognito_domain.strip()
        if cognito_domain:
            if "://" not in cognito_domain:
                if _COGNITO_DOMAIN_PREFIX_RE.fullmatch(cognito_domain) is None:
                    problems.append(
                        "COGNITO_DOMAIN must be a lowercase Cognito prefix or a credential-safe HTTPS custom-domain origin"
                    )
            else:
                try:
                    _credential_https_origin(
                        cognito_domain, setting_name="COGNITO_DOMAIN"
                    )
                except ValueError as exc:
                    problems.append(str(exc))
        if not self.cognito_callback_url.strip().lower().startswith("https://"):
            problems.append("COGNITO_CALLBACK_URL must use https://")
        if not self.frontend_base_url.strip().lower().startswith("https://"):
            problems.append("FRONTEND_BASE_URL must use https://")
        public_urls = {
            "TERMS_URL": self.terms_url,
            "PRIVACY_URL": self.privacy_url,
            "SUPPORT_URL": self.support_url,
            "STATUS_URL": self.status_url,
        }
        for name, value in public_urls.items():
            if not value.strip().lower().startswith("https://"):
                problems.append(f"{name} must use https://")
        required_providers = {
            "OPENROUTER_API_KEY": self.openrouter_api_key,
            "BACKUP_API_KEY": self.backup_api_key,
            "E2B_API_KEY": self.e2b_api_key,
            "COMPOSIO_API_KEY": self.composio_api_key,
            "GITHUB_CLIENT_ID": self.github_client_id,
            "GITHUB_CLIENT_SECRET": self.github_client_secret,
            "BROWSERBASE_API_KEY": self.browserbase_api_key,
            "BROWSERBASE_PROJECT_ID": self.browserbase_project_id,
            "SENDGRID_API_KEY": self.sendgrid_api_key,
            "NOTIFICATION_FROM_EMAIL": self.notification_from_email,
            "LANGFUSE_PUBLIC_KEY": self.langfuse_public_key,
            "LANGFUSE_SECRET_KEY": self.langfuse_secret_key,
            "SENTRY_DSN": self.sentry_dsn,
            "STRIPE_SECRET_KEY": self.stripe_secret_key,
            "STRIPE_WEBHOOK_SECRET": self.stripe_webhook_secret,
            "STRIPE_PRICE_PRO": self.stripe_price_pro,
            "STRIPE_PRICE_ENTERPRISE": self.stripe_price_enterprise,
        }
        for name, value in required_providers.items():
            if not value.strip():
                problems.append(f"{name} is required")
        try:
            openrouter_origin = _credential_https_origin(
                self.openrouter_api_base,
                setting_name="OPENROUTER_API_BASE",
                allowed_paths=frozenset({"/api/v1", "/api/v1/"}),
            )
            if (
                openrouter_origin.hostname is None
                or openrouter_origin.hostname.lower() != "openrouter.ai"
                or openrouter_origin.path.rstrip("/") != "/api/v1"
            ):
                problems.append(
                    "OPENROUTER_API_BASE must be https://openrouter.ai/api/v1"
                )
        except ValueError as exc:
            problems.append(str(exc))
        try:
            _credential_https_origin(
                self.langfuse_host, setting_name="LANGFUSE_HOST"
            )
        except ValueError as exc:
            problems.append(str(exc))
        if not 5 <= self.access_token_expire_minutes <= 1_440:
            problems.append(
                "ACCESS_TOKEN_EXPIRE_MINUTES must be between 5 and 1440 in production"
            )
        if not self.e2b_template_id.strip():
            problems.append("E2B_TEMPLATE_ID is required")
        if not self.backup_model.strip():
            problems.append("BACKUP_MODEL is required")
        elif self.backup_model.strip().lower().startswith("openrouter/"):
            problems.append(
                "BACKUP_MODEL must use a provider independent from OpenRouter"
            )
        if not self.e2b_computer_template_id.strip():
            problems.append("E2B_COMPUTER_TEMPLATE_ID is required")
        if not 300 <= self.e2b_computer_idle_timeout_seconds <= 3600:
            problems.append(
                "E2B_COMPUTER_IDLE_TIMEOUT_SECONDS must be between 300 and 3600"
            )
        if (
            not self.e2b_computer_idle_timeout_seconds
            <= self.e2b_computer_max_session_seconds
            <= 86_400
        ):
            problems.append(
                "E2B_COMPUTER_MAX_SESSION_SECONDS must be between the idle timeout and 86400"
            )
        if not 1 <= self.e2b_computer_max_active_per_member <= 10:
            problems.append(
                "E2B_COMPUTER_MAX_ACTIVE_PER_MEMBER must be between 1 and 10"
            )
        if (
            not self.e2b_computer_max_active_per_member
            <= self.e2b_computer_max_active_per_org
            <= 100
        ):
            problems.append(
                "E2B_COMPUTER_MAX_ACTIVE_PER_ORG must be between the per-member limit and 100"
            )
        if not 800 <= self.e2b_computer_screen_width <= 1920:
            problems.append("E2B_COMPUTER_SCREEN_WIDTH must be between 800 and 1920")
        if not 600 <= self.e2b_computer_screen_height <= 1200:
            problems.append("E2B_COMPUTER_SCREEN_HEIGHT must be between 600 and 1200")
        if not self.browserbase_operator_enabled:
            problems.append("BROWSERBASE_OPERATOR_ENABLED must be true")
        if self.browserbase_region not in {
            "us-west-2",
            "us-east-1",
            "eu-central-1",
            "ap-southeast-1",
        }:
            problems.append("BROWSERBASE_REGION is invalid")
        if not 60 <= self.browserbase_session_timeout_seconds <= 21_600:
            problems.append(
                "BROWSERBASE_SESSION_TIMEOUT_SECONDS must be between 60 and 21600"
            )
        if not self.e2b_repo_enabled:
            problems.append("E2B_REPO_ENABLED must be true")
        if not self.e2b_repo_template_id.strip():
            problems.append("E2B_REPO_TEMPLATE_ID is required")
        if not self.e2b_repo_allow_internet_access:
            problems.append(
                "E2B_REPO_ALLOW_INTERNET_ACCESS must be true for Git and package access"
            )
        from core.egress_policy import parse_egress_allowlist

        if self.e2b_computer_allow_internet_access and not parse_egress_allowlist(
            self.e2b_computer_egress_allowlist
        ):
            problems.append(
                "E2B_COMPUTER_EGRESS_ALLOWLIST is required when cloud computer network access is enabled"
            )
        if self.e2b_repo_allow_internet_access and not parse_egress_allowlist(
            self.e2b_repo_egress_allowlist
        ):
            problems.append(
                "E2B_REPO_EGRESS_ALLOWLIST is required when repository network access is enabled"
            )
        if not self.malware_scan_required:
            problems.append("MALWARE_SCAN_REQUIRED must be true")
        if not 300 <= self.e2b_repo_timeout_seconds <= 21_600:
            problems.append("E2B_REPO_TIMEOUT_SECONDS must be between 300 and 21600")
        if not 10 <= self.e2b_repo_command_timeout_seconds <= 600:
            problems.append(
                "E2B_REPO_COMMAND_TIMEOUT_SECONDS must be between 10 and 600"
            )
        if not 1_048_576 <= self.e2b_repo_max_snapshot_bytes <= 104_857_600:
            problems.append(
                "E2B_REPO_MAX_SNAPSHOT_BYTES must be between 1048576 and 104857600"
            )
        if not 1 <= self.e2b_repo_max_workspaces_per_org <= 500:
            problems.append("E2B_REPO_MAX_WORKSPACES_PER_ORG must be between 1 and 500")
        if not 1 <= self.e2b_repo_max_workspaces_per_task <= 10:
            problems.append("E2B_REPO_MAX_WORKSPACES_PER_TASK must be between 1 and 10")
        if problems:
            raise ValueError(
                "Insecure configuration for ENVIRONMENT="
                f"{self.environment}: " + "; ".join(problems)
            )
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

    # Authorization (OpenFGA). Enforcement is ON BY DEFAULT: the moment an operator
    # configures OpenFGA (sets openfga_api_url), permission.check queries it and
    # raises PermissionDenied on a deny — no separate opt-in flag needed. Setting
    # permissions_enforce=false is the explicit kill-switch to fall back to the
    # allow-all stub for FGA-mapped actions. The deterministic role gates (approval
    # decisions, admin governance mutations) are always enforced regardless.
    permissions_enforce: bool = True
    openfga_api_url: str = ""  # e.g. http://localhost:8080 — empty disables
    openfga_api_token: str = ""  # pre-shared key sent as Authorization: Bearer
    openfga_store_id: str = ""  # resolved/created at bootstrap if empty
    openfga_model_id: str = ""  # resolved/written at bootstrap if empty

    # Sub-agent concurrency
    concurrent_sub_agents: int = 5

    # Agent-loop cognition (plan / reflect / dynamic routing). When disabled the
    # loop falls back to the proven model-native behavior. The planner/critic
    # also no-op automatically when no model API key is configured.
    agent_cognition_enabled: bool = True
    agent_max_reflections: int = 2
    agent_max_replans: int = 3

    # Context budgeting (category 7)
    max_context_tokens: int = 120_000  # conservative for frontier models
    response_reserve_tokens: int = 4_000

    # Per-org token budget guard (category 9) — 0 means unlimited
    per_org_daily_token_limit: int = 0

    # Billing (Stripe) — empty disables billing (truthful-degraded).
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_pro: str = ""
    stripe_price_enterprise: str = ""

    @model_validator(mode="after")
    def validate_stripe_billing(self) -> "Settings":
        """Keep billing either fully disabled or safe to expose to customers."""
        values = {
            "STRIPE_SECRET_KEY": self.stripe_secret_key,
            "STRIPE_WEBHOOK_SECRET": self.stripe_webhook_secret,
            "STRIPE_PRICE_PRO": self.stripe_price_pro,
            "STRIPE_PRICE_ENTERPRISE": self.stripe_price_enterprise,
        }
        configured = {name for name, value in values.items() if value.strip()}
        if configured and len(configured) != len(values):
            missing = sorted(set(values) - configured)
            raise ValueError(
                "Stripe billing must configure all four values together; missing "
                + ", ".join(missing)
            )
        if (
            len(configured) == len(values)
            and self.stripe_price_pro == self.stripe_price_enterprise
        ):
            raise ValueError(
                "STRIPE_PRICE_PRO and STRIPE_PRICE_ENTERPRISE must be distinct"
            )
        return self

    # Notification email delivery (W5.3) — empty disables email delivery
    # (truthful-degraded; in-app notifications still work). When set, the
    # provider seam in core/notification_delivery.py sends from this address.
    sendgrid_api_key: str = ""
    notification_from_email: str = ""

    # Observability (category 10)
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    sentry_dsn: str = ""

    # Resolve the repository env file from this module, never from process cwd.
    # The old cwd-relative tuple could silently load an unrelated ~/ .env after
    # the project file and override DATABASE_URL or provider credentials.
    # Container production injects environment variables directly; /app/.env is
    # intentionally absent there.
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
