import pytest

from core.config import Settings


def test_object_storage_defaults_to_local_minio() -> None:
    settings = Settings(_env_file=None)

    assert settings.object_storage_is_s3 is False
    assert settings.object_storage_health_name == "minio"
    assert settings.object_storage_endpoint == "localhost:9000"
    assert settings.object_storage_bucket == "chronos"
    assert settings.object_storage_access_key == "chronos"
    assert settings.object_storage_secret_key == "chronos123"
    assert settings.object_storage_session_token == ""
    assert settings.object_storage_secure is False
    assert settings.object_storage_region is None
    assert settings.object_storage_bucket_location is None


def test_object_storage_s3_uses_aws_settings() -> None:
    settings = Settings(
        _env_file=None,
        object_storage_backend="s3",
        aws_s3_bucket="chronos-prod",
        aws_s3_region="us-west-2",
        aws_access_key_id="AKIA_TEST",
        aws_secret_access_key="secret",
        aws_session_token="session",
    )

    assert settings.object_storage_is_s3 is True
    assert settings.object_storage_health_name == "s3"
    assert settings.object_storage_endpoint == "s3.us-west-2.amazonaws.com"
    assert settings.object_storage_bucket == "chronos-prod"
    assert settings.object_storage_access_key == "AKIA_TEST"
    assert settings.object_storage_secret_key == "secret"
    assert settings.object_storage_session_token == "session"
    assert settings.object_storage_secure is True
    assert settings.object_storage_region == "us-west-2"
    assert settings.object_storage_bucket_location == "us-west-2"


def test_object_storage_s3_us_east_1_omits_create_bucket_location() -> None:
    settings = Settings(
        _env_file=None,
        object_storage_backend="s3",
        aws_s3_bucket="chronos-prod",
        aws_s3_region="us-east-1",
    )

    assert settings.object_storage_endpoint == "s3.us-east-1.amazonaws.com"
    assert settings.object_storage_bucket_location is None


def test_object_storage_s3_can_use_custom_compatible_endpoint() -> None:
    settings = Settings(
        _env_file=None,
        object_storage_backend="s3",
        aws_s3_bucket="chronos-prod",
        aws_s3_endpoint="s3.example.internal",
    )

    assert settings.object_storage_endpoint == "s3.example.internal"


def test_object_storage_s3_requires_bucket() -> None:
    with pytest.raises(ValueError, match="AWS_S3_BUCKET"):
        Settings(_env_file=None, object_storage_backend="s3")


def test_object_storage_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="OBJECT_STORAGE_BACKEND"):
        Settings(_env_file=None, object_storage_backend="filesystem")
