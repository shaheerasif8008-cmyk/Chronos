"""W1 Phase 2B-2 — per-subdomain login resolves the member's own org."""
from __future__ import annotations

import time
import uuid
import httpx
import pytest

import main
from core import auth as auth_core
from core.config import settings
from core.db import engine, reflect_table
from routers.auth import _otp_store


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _make_org_member(subdomain: str, email: str):
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=subdomain, subdomain=subdomain, name="T"))
        await conn.execute(members.insert().values(
            id=member_id, organization_id=org_id, email=email.lower(), role="owner",
        ))
    return org_id, member_id


@pytest.mark.asyncio
async def test_login_resolves_member_in_subdomain_org():
    sub = f"acme{uuid.uuid4().hex[:8]}"
    email = f"founder@{sub}.io"
    org_id, member_id = await _make_org_member(sub, email)
    _otp_store[email.lower()] = {"code": "123456", "expires_at": time.time() + 300, "attempts": 0}
    async with _client() as client:
        resp = await client.post("/auth/verify-otp", json={"email": email, "code": "123456"},
                                 headers={"X-Chronos-Org": sub})
    assert resp.status_code == 200
    assert resp.json()["member_id"] == member_id


@pytest.mark.asyncio
async def test_login_rejects_member_not_in_resolved_org():
    sub = f"globex{uuid.uuid4().hex[:8]}"
    await _make_org_member(sub, f"someone@{sub}.io")
    stranger = f"stranger{uuid.uuid4().hex[:6]}@nope.io"
    _otp_store[stranger.lower()] = {"code": "123456", "expires_at": time.time() + 300, "attempts": 0}
    async with _client() as client:
        resp = await client.post("/auth/verify-otp", json={"email": stranger, "code": "123456"},
                                 headers={"X-Chronos-Org": sub})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_logout_clears_cookie_and_invalidates_browser_session():
    sub = f"logout{uuid.uuid4().hex[:8]}"
    email = f"owner@{sub}.io"
    await _make_org_member(sub, email)
    _otp_store[email.lower()] = {"code": "654321", "expires_at": time.time() + 300, "attempts": 0}

    async with _client() as client:
        login = await client.post(
            "/auth/verify-otp",
            json={"email": email, "code": "654321"},
            headers={"X-Chronos-Org": sub},
        )
        assert login.status_code == 200
        assert (await client.get("/auth/me", headers={"X-Chronos-Org": sub})).status_code == 200

        logout = await client.post(
            "/auth/logout",
            headers={"X-Chronos-Org": sub, "Origin": "http://localhost:3000"},
        )
        assert logout.status_code == 200
        assert logout.json() == {"status": "signed_out"}
        assert "chronos_session=" in logout.headers["set-cookie"]
        assert "Max-Age=0" in logout.headers["set-cookie"]
        assert (await client.get("/auth/me", headers={"X-Chronos-Org": sub})).status_code == 401


def test_cognito_tenant_state_roundtrip_and_tamper_rejection():
    state = auth_core.create_cognito_oauth_state(org_id="org-1", subdomain="novatech")
    claims = auth_core.decode_cognito_oauth_state(state)
    assert claims["org"] == "org-1"
    assert claims["tenant"] == "novatech"
    with pytest.raises(ValueError, match="Invalid or expired"):
        auth_core.decode_cognito_oauth_state(state + "tamper")


def _enable_production_cognito(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "auth_provider", "cognito")
    monkeypatch.setattr(settings, "base_domain", "cognisiatech.com")
    monkeypatch.setattr(settings, "cognito_region", "us-east-2")
    monkeypatch.setattr(settings, "cognito_user_pool_id", "us-east-2_test")
    monkeypatch.setattr(settings, "cognito_app_client_id", "client-test")
    monkeypatch.setattr(settings, "cognito_domain", "chronos-test")
    monkeypatch.setattr(
        settings,
        "cognito_callback_url",
        "https://app.cognisiatech.com/login/callback",
    )


@pytest.mark.asyncio
async def test_auth_config_binds_cognito_login_to_requested_tenant(monkeypatch):
    sub = f"tenant{uuid.uuid4().hex[:8]}"
    org_id, _ = await _make_org_member(sub, f"owner@{sub}.io")
    _enable_production_cognito(monkeypatch)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="https://api.cognisiatech.com",
    ) as client:
        response = await client.get(f"/auth/config?tenant={sub}")

    assert response.status_code == 200
    body = response.json()
    assert body["cognito"]["tenant"] == sub
    assert body["cognito"]["requiresTenant"] is True
    assert "state=" in body["cognito"]["loginUrl"]
    cookie = response.headers["set-cookie"]
    assert "chronos_oauth_state=" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie
    state = body["cognito"]["loginUrl"].split("state=", 1)[1].split("&", 1)[0]
    from urllib.parse import unquote

    assert auth_core.decode_cognito_oauth_state(unquote(state))["org"] == org_id


@pytest.mark.asyncio
async def test_cognito_callback_uses_signed_tenant_and_returns_tenant_redirect(monkeypatch):
    sub = f"callback{uuid.uuid4().hex[:8]}"
    email = f"owner@{sub}.io"
    org_id, member_id = await _make_org_member(sub, email)
    _enable_production_cognito(monkeypatch)
    state = auth_core.create_cognito_oauth_state(org_id=org_id, subdomain=sub)

    async def fake_exchange(_code: str, *, redirect_uri: str | None = None):
        assert redirect_uri == "https://app.cognisiatech.com/login/callback"
        return {"claims": {"email": email, "name": "Owner"}}

    monkeypatch.setattr("routers.auth.exchange_authorization_code", fake_exchange)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="https://api.cognisiatech.com",
        cookies={"chronos_oauth_state": state},
    ) as client:
        response = await client.post(
            "/auth/cognito/callback",
            json={
                "code": "one-time-code",
                "state": state,
                "redirect_uri": "https://app.cognisiatech.com/login/callback",
            },
        )

    assert response.status_code == 200
    assert response.json()["member_id"] == member_id
    assert response.json()["redirect_url"] == f"https://{sub}.cognisiatech.com/chat"


@pytest.mark.asyncio
async def test_cognito_callback_rejects_browser_state_mismatch(monkeypatch):
    sub = f"mismatch{uuid.uuid4().hex[:8]}"
    org_id, _ = await _make_org_member(sub, f"owner@{sub}.io")
    _enable_production_cognito(monkeypatch)
    state = auth_core.create_cognito_oauth_state(org_id=org_id, subdomain=sub)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="https://api.cognisiatech.com",
        cookies={"chronos_oauth_state": "different"},
    ) as client:
        response = await client.post(
            "/auth/cognito/callback",
            json={
                "code": "unused",
                "state": state,
                "redirect_uri": "https://app.cognisiatech.com/login/callback",
            },
        )
    assert response.status_code == 400
    assert "did not match" in response.json()["detail"]
