from pathlib import Path

from core.config import Settings


def test_env_example_documents_every_settings_field() -> None:
    """Every operator-facing setting must be discoverable in the env template."""

    repo_root = Path(__file__).resolve().parents[3]
    configured_keys = {
        line.split("=", 1)[0].strip()
        for line in (repo_root / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and line.split("=", 1)[0].strip().replace("_", "").isalnum()
    }
    expected_keys = {field.upper() for field in Settings.model_fields}

    assert expected_keys <= configured_keys, (
        "Add every new Settings field to .env.example; missing: "
        + ", ".join(sorted(expected_keys - configured_keys))
    )

    expected_web_build_keys = {
        "NEXT_PUBLIC_API_BASE_URL",
        "NEXT_PUBLIC_WEB_BASE_URL",
        "NEXT_PUBLIC_TERMS_URL",
        "NEXT_PUBLIC_PRIVACY_URL",
        "NEXT_PUBLIC_SUPPORT_URL",
        "NEXT_PUBLIC_STATUS_URL",
        "NEXT_PUBLIC_SENTRY_DSN",
        "NEXT_PUBLIC_CHRONOS_ENVIRONMENT",
        "NEXT_PUBLIC_CHRONOS_RELEASE",
    }
    assert expected_web_build_keys <= configured_keys, (
        "Document every production web build setting in .env.example; missing: "
        + ", ".join(sorted(expected_web_build_keys - configured_keys))
    )
