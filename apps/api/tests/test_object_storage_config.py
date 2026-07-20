import pytest

from core.config import Settings
from core.object_storage import check_bucket_sync


def test_object_storage_defaults_to_s3_when_bucket_is_configured() -> None:
    settings = Settings(
        _env_file=None,
        aws_s3_bucket="chronos-prod",
        aws_s3_endpoint="",
        aws_access_key_id="",
        aws_secret_access_key="",
        aws_session_token="",
    )

    assert settings.object_storage_is_s3 is True
    assert settings.object_storage_health_name == "s3"
    assert settings.object_storage_endpoint == "s3.us-east-1.amazonaws.com"
    assert settings.object_storage_bucket == "chronos-prod"
    assert settings.object_storage_access_key == ""
    assert settings.object_storage_secret_key == ""
    assert settings.object_storage_session_token == ""
    assert settings.object_storage_secure is True
    assert settings.object_storage_region == "us-east-1"
    assert settings.object_storage_bucket_location is None


def test_object_storage_s3_uses_aws_settings() -> None:
    settings = Settings(
        _env_file=None,
        object_storage_backend="s3",
        aws_s3_bucket="chronos-prod",
        aws_s3_region="us-west-2",
        aws_s3_endpoint="",
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
        aws_s3_endpoint="",
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


def test_object_storage_s3_requires_bucket(monkeypatch) -> None:
    # _env_file=None skips the dotenv file but NOT process env vars; CI/dev
    # shells export AWS_S3_BUCKET, which would satisfy the validator.
    monkeypatch.delenv("AWS_S3_BUCKET", raising=False)
    with pytest.raises(ValueError, match="AWS_S3_BUCKET"):
        Settings(_env_file=None)


def test_object_storage_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="OBJECT_STORAGE_BACKEND"):
        Settings(_env_file=None, object_storage_backend="filesystem")


def test_object_storage_health_check_never_creates_a_missing_bucket() -> None:
    class Client:
        create_calls = 0

        def head_bucket(self, **_kwargs):
            raise RuntimeError("not found")

        def create_bucket(self, **_kwargs):
            self.create_calls += 1

    client = Client()
    with pytest.raises(RuntimeError, match="not found"):
        check_bucket_sync(client)
    assert client.create_calls == 0
