import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import pytest

from core import cognito
from core.config import settings


def _rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture
def cognito_settings(monkeypatch):
    monkeypatch.setattr(settings, "auth_provider", "cognito")
    monkeypatch.setattr(settings, "cognito_region", "us-east-1")
    monkeypatch.setattr(settings, "cognito_user_pool_id", "us-east-1_TestPool")
    monkeypatch.setattr(settings, "cognito_app_client_id", "test-client-id")
    monkeypatch.setattr(settings, "cognito_domain", "chronos-dev")
    cognito._jwks_client.cache_clear()


def test_cognito_enabled_requires_pool_and_client(monkeypatch):
    monkeypatch.setattr(settings, "auth_provider", "cognito")
    monkeypatch.setattr(settings, "cognito_user_pool_id", "")
    assert cognito.cognito_enabled() is False


def test_email_from_claims_requires_email():
    with pytest.raises(cognito.CognitoAuthError):
        cognito.email_from_claims({"sub": "abc"})


def test_email_from_claims_normalizes():
    assert cognito.email_from_claims({"email": "Admin@Example.com"}) == "admin@example.com"


def test_verify_id_token(monkeypatch, cognito_settings):
    private_pem, public_pem = _rsa_keypair()

    class FakeSigningKey:
        def __init__(self, key):
            self.key = key

    class FakeJWKClient:
        def get_signing_key_from_jwt(self, _token):
            return FakeSigningKey(public_pem)

    monkeypatch.setattr(cognito, "_jwks_client", lambda: FakeJWKClient())

    token = jwt.encode(
        {
            "sub": "user-1",
            "email": "admin@example.com",
            "token_use": "id",
            "aud": settings.cognito_app_client_id,
            "iss": cognito.issuer(),
            "exp": 9_999_999_999,
            "iat": 1_700_000_000,
        },
        private_pem,
        algorithm="RS256",
    )

    claims = cognito.verify_id_token(token)
    assert claims["email"] == "admin@example.com"


def test_build_authorize_url(cognito_settings):
    url = cognito.build_authorize_url()
    assert "oauth2/authorize" in url
    assert settings.cognito_app_client_id in url
    assert "redirect_uri=" in url
