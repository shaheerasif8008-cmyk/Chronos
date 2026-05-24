"""Cognito auth integration tests.

All network calls (Cognito token endpoint, JWKS) and DB writes are mocked at
the function boundary — the established pattern in this suite.
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest


# ── verify_cognito_token ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_cognito_token_accepts_valid_token(monkeypatch):
    """A well-formed, properly-signed ID token should decode without error."""
    from core import auth as auth_mod

    good_claims = {
        "sub": "us-east-2:abc123",
        "email": "alice@example.com",
        "token_use": "id",
        "aud": "test-client-id",
        "iss": "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_POOL",
        "exp": int(time.time()) + 3600,
    }

    # Stub the synchronous inner function so no real RSA key or JWKS is needed
    monkeypatch.setattr(auth_mod, "_verify_cognito_token_sync", lambda token: good_claims)
    monkeypatch.setattr(auth_mod.settings, "cognito_user_pool_id", "us-east-2_POOL")

    claims = await auth_mod.verify_cognito_token("fake.token.value")
    assert claims["email"] == "alice@example.com"
    assert claims["token_use"] == "id"


@pytest.mark.asyncio
async def test_verify_cognito_token_raises_when_not_configured(monkeypatch):
    from core import auth as auth_mod

    monkeypatch.setattr(auth_mod.settings, "cognito_user_pool_id", "")

    with pytest.raises(ValueError, match="not configured"):
        await auth_mod.verify_cognito_token("any.token")


@pytest.mark.asyncio
async def test_verify_cognito_token_raises_on_bad_token(monkeypatch):
    from core import auth as auth_mod

    monkeypatch.setattr(auth_mod.settings, "cognito_user_pool_id", "us-east-2_POOL")

    # Simulate a PyJWTError from the inner sync function
    def raise_jwt_error(token):
        raise jwt.InvalidTokenError("bad signature")

    monkeypatch.setattr(auth_mod, "_verify_cognito_token_sync", raise_jwt_error)

    with pytest.raises(ValueError, match="Token verification"):
        await auth_mod.verify_cognito_token("bad.token")


# ── GET /auth/cognito/authorize ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cognito_authorize_returns_url(monkeypatch):
    """Authorize URL is built from settings and contains required OAuth params."""
    import routers.auth as auth_router_mod
    from core import config as cfg_mod

    monkeypatch.setenv("COGNITO_DOMAIN", "https://example.auth.us-east-2.amazoncognito.com")
    monkeypatch.setenv("COGNITO_APP_CLIENT_ID", "test-client")
    monkeypatch.setenv("COGNITO_CALLBACK_URL", "http://localhost:3000/login/callback")
    cfg_mod.get_settings.cache_clear()
    monkeypatch.setattr(auth_router_mod, "settings", cfg_mod.get_settings())

    from routers.auth import cognito_authorize

    body = await cognito_authorize()

    assert "authorize_url" in body
    assert "response_type=code" in body["authorize_url"]
    assert "test-client" in body["authorize_url"]
    assert "openid" in body["authorize_url"]
    assert "example.auth.us-east-2.amazoncognito.com" in body["authorize_url"]


# ── POST /auth/cognito/callback ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cognito_callback_full_flow(monkeypatch):
    """Code exchange → token verification → member lookup → Chronos JWT issued."""
    import sqlalchemy as sa
    from fastapi import Response

    import routers.auth as auth_router
    from core import audit as audit_mod
    from core import config as cfg_mod

    # Patch settings so Cognito is considered configured
    cfg_mod.get_settings.cache_clear()
    monkeypatch.setenv("COGNITO_DOMAIN", "https://example.auth.us-east-2.amazoncognito.com")
    monkeypatch.setenv("COGNITO_APP_CLIENT_ID", "client-id")
    monkeypatch.setenv("COGNITO_APP_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("COGNITO_CALLBACK_URL", "http://localhost:3000/login/callback")
    cfg_mod.get_settings.cache_clear()
    monkeypatch.setattr(auth_router, "settings", cfg_mod.get_settings())

    # Mock Cognito token endpoint
    async def fake_http_post(self_cls, url, *, data, headers):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id_token": "fake.id.token", "access_token": "at"}
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    import httpx as httpx_mod
    monkeypatch.setattr(httpx_mod.AsyncClient, "post", fake_http_post)

    # Mock verify_cognito_token — skip real JWKS / RSA
    async def fake_verify(token: str):
        return {"email": "alice@example.com", "sub": "cognito-abc", "token_use": "id"}

    monkeypatch.setattr(auth_router, "verify_cognito_token", fake_verify)
    monkeypatch.setattr(audit_mod, "log", AsyncMock())

    existing_member = {
        "id": "member-uuid-001", "email": "alice@example.com",
        "organization_id": "default", "role": "user", "name": "Alice", "region": "us",
    }

    class FakeResult:
        def mappings(self): return self
        def first(self): return existing_member

    class FakeConn:
        async def execute(self, stmt, *a): return FakeResult()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class FakeEngine:
        def begin(self): return FakeConn()

    fake_table = sa.Table(
        "members", sa.MetaData(),
        sa.Column("email", sa.Text()), sa.Column("id", sa.Text()),
    )
    monkeypatch.setattr(auth_router, "reflect_table", AsyncMock(return_value=fake_table))
    monkeypatch.setattr(auth_router, "engine", FakeEngine())

    from routers.auth import CognitoCallbackRequest, cognito_callback

    response = Response()
    result = await cognito_callback(CognitoCallbackRequest(code="auth-code-xyz"), response)

    assert "access_token" in result
    assert result["member_id"] == "member-uuid-001"
    assert result["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_cognito_callback_rejects_missing_code(monkeypatch):
    """Calling the endpoint function without a code raises a Pydantic error."""
    from pydantic import ValidationError
    from routers.auth import CognitoCallbackRequest

    with pytest.raises((ValidationError, TypeError)):
        CognitoCallbackRequest()  # type: ignore[call-arg]


# ── OTP endpoints disabled when Cognito is configured ─────────────────────────

@pytest.mark.asyncio
async def test_otp_disabled_when_cognito_configured(monkeypatch):
    from fastapi import HTTPException

    import routers.auth as auth_router
    from core import config as cfg_mod

    cfg_mod.get_settings.cache_clear()
    monkeypatch.setenv("COGNITO_DOMAIN", "https://example.auth.us-east-2.amazoncognito.com")
    cfg_mod.get_settings.cache_clear()
    monkeypatch.setattr(auth_router, "settings", cfg_mod.get_settings())

    from routers.auth import OtpRequest, request_otp

    with pytest.raises(HTTPException) as exc_info:
        await request_otp(OtpRequest(email="dev@test.com"))

    assert exc_info.value.status_code == 404
    assert "disabled" in exc_info.value.detail.lower()
