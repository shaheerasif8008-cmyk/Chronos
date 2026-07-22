from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import Settings


def _valid_production_settings() -> dict[str, object]:
    return {
        "_env_file": None,
        "environment": "production",
        "auth_provider": "cognito",
        "jwt_secret": "j" * 40,
        "vault_encryption_key": "a" * 64,
        "aws_s3_bucket": "chronos-production-artifacts",
        "aws_s3_endpoint": "",
        "aws_access_key_id": "",
        "aws_secret_access_key": "",
        "db_ssl_mode": "require",
        "redis_url": "rediss://:password@redis.internal:6379/0",
        "openfga_api_url": "http://openfga.internal:8080",
        "openfga_api_token": "f" * 40,
        "per_org_daily_token_limit": 100_000,
        "cognito_user_pool_id": "us-east-1_example",
        "cognito_app_client_id": "client-id",
        "cognito_domain": "https://auth.cognisiatech.com",
        "cognito_callback_url": "https://app.cognisiatech.com/login/callback",
        "frontend_base_url": "https://app.cognisiatech.com",
        "terms_url": "https://www.cognisiatech.com/terms",
        "privacy_url": "https://www.cognisiatech.com/privacy",
        "support_url": "https://www.cognisiatech.com/support",
        "status_url": "https://status.cognisiatech.com",
        "openrouter_api_key": "openrouter-key",
        "backup_api_key": "anthropic-key",
        "backup_model": "anthropic/claude-sonnet-5",
        "e2b_api_key": "e2b-key",
        "e2b_template_id": "execution-template",
        "e2b_computer_template_id": "desktop-template",
        "e2b_computer_egress_allowlist": "github.com,api.github.com",
        "e2b_repo_enabled": True,
        "e2b_repo_template_id": "repo-template",
        "e2b_repo_allow_internet_access": True,
        "e2b_repo_egress_allowlist": "github.com,pypi.org,files.pythonhosted.org",
        "composio_api_key": "composio-key",
        "github_client_id": "github-client",
        "github_client_secret": "github-secret",
        "browserbase_api_key": "browserbase-key",
        "browserbase_operator_enabled": True,
        "browserbase_project_id": "browserbase-project",
        "sendgrid_api_key": "sendgrid-key",
        "notification_from_email": "notifications@cognisiatech.com",
        "langfuse_public_key": "langfuse-public",
        "langfuse_secret_key": "langfuse-secret",
        "sentry_dsn": "https://public@example.ingest.sentry.io/1",
        "stripe_secret_key": "sk_live_test",
        "stripe_webhook_secret": "whsec_test",
        "stripe_price_pro": "price_pro",
        "stripe_price_enterprise": "price_enterprise",
        "malware_scan_required": True,
    }


def test_settings_never_load_unrelated_cwd_env(tmp_path: Path, monkeypatch):
    (tmp_path / ".env").write_text(
        "AWS_S3_BUCKET=attacker-controlled-bucket\n"
        "DATABASE_URL=postgresql+asyncpg://wrong:wrong@localhost:1/wrong\n"
    )
    monkeypatch.chdir(tmp_path)
    # Provide a legitimate bucket via the real environment so the object-storage
    # validator is satisfied; the assertions below then prove the stray cwd .env
    # was ignored in favor of the real environment.
    monkeypatch.setenv("AWS_S3_BUCKET", "legit-env-bucket")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    loaded = Settings()

    assert loaded.aws_s3_bucket == "legit-env-bucket"
    assert loaded.aws_s3_bucket != "attacker-controlled-bucket"
    assert "localhost:1/wrong" not in loaded.database_url


@pytest.mark.parametrize(
    ("jwt_secret", "vault_key"),
    [
        ("too-short", "a" * 64),
        ("x" * 32, "not-hex" * 9 + "x"),
    ],
)
def test_production_rejects_weak_or_malformed_cryptographic_secrets(
    jwt_secret, vault_key
):
    with pytest.raises(ValidationError, match="Insecure configuration"):
        Settings(
            environment="production",
            auth_provider="cognito",
            aws_s3_bucket="production-bucket",
            jwt_secret=jwt_secret,
            vault_encryption_key=vault_key,
        )


def test_production_accepts_only_the_complete_fail_closed_runtime_contract():
    loaded = Settings(**_valid_production_settings())
    assert loaded.is_production is True


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"demo_mode": True}, "DEMO_MODE"),
        ({"permissions_enforce": False}, "PERMISSIONS_ENFORCE"),
        ({"openfga_api_url": ""}, "OPENFGA_API_URL"),
        ({"openfga_api_token": "short"}, "OPENFGA_API_TOKEN"),
        ({"enforce_org_bound_tokens": False}, "ENFORCE_ORG_BOUND_TOKENS"),
        ({"per_org_daily_token_limit": 0}, "PER_ORG_DAILY_TOKEN_LIMIT"),
        ({"aws_s3_endpoint": "http://minio:9000"}, "AWS_S3_ENDPOINT"),
        (
            {"aws_access_key_id": "static", "aws_secret_access_key": "static"},
            "static AWS",
        ),
        ({"db_ssl_mode": ""}, "DB_SSL_MODE"),
        ({"redis_url": "redis://redis.internal:6379"}, "REDIS_URL"),
        ({"cognito_domain": "http://auth.example.com"}, "COGNITO_DOMAIN"),
        ({"cognito_domain": "https://auth.example.com/tenant"}, "COGNITO_DOMAIN"),
        ({"cognito_domain": "auth.example.com"}, "COGNITO_DOMAIN"),
        (
            {"cognito_callback_url": "http://app.example/login/callback"},
            "COGNITO_CALLBACK_URL",
        ),
        ({"frontend_base_url": "http://app.example"}, "FRONTEND_BASE_URL"),
        ({"terms_url": ""}, "TERMS_URL"),
        ({"privacy_url": "http://example.com/privacy"}, "PRIVACY_URL"),
        ({"support_url": "mailto:support@example.com"}, "SUPPORT_URL"),
        ({"status_url": ""}, "STATUS_URL"),
        ({"e2b_api_key": ""}, "E2B_API_KEY"),
        ({"e2b_template_id": ""}, "E2B_TEMPLATE_ID"),
        ({"backup_api_key": ""}, "BACKUP_API_KEY"),
        ({"backup_model": "openrouter/openai/gpt-5.4-mini"}, "BACKUP_MODEL"),
        ({"e2b_computer_template_id": ""}, "E2B_COMPUTER_TEMPLATE_ID"),
        (
            {"e2b_computer_idle_timeout_seconds": 299},
            "E2B_COMPUTER_IDLE_TIMEOUT_SECONDS",
        ),
        ({"e2b_computer_max_session_seconds": 200}, "E2B_COMPUTER_MAX_SESSION_SECONDS"),
        (
            {"e2b_computer_max_active_per_member": 0},
            "E2B_COMPUTER_MAX_ACTIVE_PER_MEMBER",
        ),
        (
            {
                "e2b_computer_max_active_per_member": 3,
                "e2b_computer_max_active_per_org": 2,
            },
            "E2B_COMPUTER_MAX_ACTIVE_PER_ORG",
        ),
        ({"e2b_computer_screen_width": 799}, "E2B_COMPUTER_SCREEN_WIDTH"),
        ({"e2b_repo_enabled": False}, "E2B_REPO_ENABLED"),
        ({"e2b_repo_template_id": ""}, "E2B_REPO_TEMPLATE_ID"),
        ({"e2b_repo_allow_internet_access": False}, "E2B_REPO_ALLOW_INTERNET_ACCESS"),
        ({"malware_scan_required": False}, "MALWARE_SCAN_REQUIRED"),
        ({"sendgrid_api_key": ""}, "SENDGRID_API_KEY"),
        ({"github_client_id": ""}, "GITHUB_CLIENT_ID"),
        ({"github_client_secret": ""}, "GITHUB_CLIENT_SECRET"),
        ({"browserbase_operator_enabled": False}, "BROWSERBASE_OPERATOR_ENABLED"),
        ({"browserbase_project_id": ""}, "BROWSERBASE_PROJECT_ID"),
        ({"stripe_secret_key": ""}, "STRIPE_SECRET_KEY"),
        (
            {"openrouter_api_base": "https://openrouter.ai.evil.example/api/v1"},
            "OPENROUTER_API_BASE",
        ),
        (
            {"openrouter_api_base": "https://openrouter.ai/v1"},
            "OPENROUTER_API_BASE",
        ),
        ({"langfuse_host": "http://langfuse.example.com"}, "LANGFUSE_HOST"),
        (
            {"langfuse_host": "https://key@langfuse.example.com"},
            "LANGFUSE_HOST",
        ),
        ({"access_token_expire_minutes": 1}, "ACCESS_TOKEN_EXPIRE_MINUTES"),
        ({"access_token_expire_minutes": 1_441}, "ACCESS_TOKEN_EXPIRE_MINUTES"),
    ],
)
def test_production_rejects_insecure_runtime_drift(override, expected):
    values = _valid_production_settings()
    values.update(override)
    with pytest.raises(ValidationError, match=expected):
        Settings(**values)


@pytest.mark.parametrize(
    "cognito_domain",
    [
        "chronos-prod",
        "https://auth.cognisiatech.com",
        "https://chronos-prod.auth.us-east-1.amazoncognito.com/",
    ],
)
def test_production_accepts_cognito_hosted_prefix_and_https_custom_domains(
    cognito_domain,
):
    values = _valid_production_settings()
    values["cognito_domain"] = cognito_domain

    loaded = Settings(**values)

    assert loaded.cognito_domain == cognito_domain


def test_local_development_keeps_non_https_provider_endpoints_available():
    loaded = Settings(
        _env_file=None,
        environment="development",
        aws_s3_bucket="local-bucket",
        cognito_domain="http://localhost:3001",
        openrouter_api_base="http://localhost:4000/v1",
        langfuse_host="http://localhost:3000",
        access_token_expire_minutes=2,
    )

    assert loaded.is_production is False
