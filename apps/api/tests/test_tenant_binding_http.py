"""W1 Phase 1 — org-bound session tokens carry and enforce an `org` claim."""
from __future__ import annotations

import uuid

import jwt
import pytest
import httpx
import main

from core.auth import create_access_token
from core.config import settings
from core.db import engine, reflect_table


def test_token_includes_org_claim_when_provided():
    token = create_access_token("member-123", org_id="org-abc")
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    assert payload["org"] == "org-abc"


def test_token_omits_org_claim_when_not_provided():
    token = create_access_token("member-123")
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    assert "org" not in payload  # legacy tokens stay org-less and grandfathered


async def _make_org_and_member(subdomain: str):
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(
            id=org_id, slug=subdomain, subdomain=subdomain, name=subdomain.title(),
        ))
        await conn.execute(members.insert().values(
            id=member_id, organization_id=org_id, email=f"{member_id[:8]}@{subdomain}.io", role="owner",
        ))
    return org_id, member_id


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _subdomain_of(org_id: str) -> str:
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        return (await conn.execute(orgs.select().where(orgs.c.id == org_id))).mappings().one()["subdomain"]


@pytest.mark.asyncio
async def test_org_bound_token_rejected_on_wrong_tenant():
    org_a, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    org_b, _ = await _make_org_and_member(f"globex{uuid.uuid4().hex[:6]}")
    token = create_access_token(member_a, org_id=org_a)
    b_label = await _subdomain_of(org_b)
    async with _client() as client:
        resp = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": b_label},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Token not valid for this tenant"


@pytest.mark.asyncio
async def test_org_bound_token_accepted_on_its_own_tenant():
    org_a, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    token = create_access_token(member_a, org_id=org_a)
    a_label = await _subdomain_of(org_a)
    async with _client() as client:
        resp = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": a_label},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_legacy_token_without_org_claim_is_grandfathered():
    _, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    token = create_access_token(member_a)  # no org claim
    async with _client() as client:
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_org_bound_cookie_token_rejected_on_wrong_tenant():
    org_a, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    org_b, _ = await _make_org_and_member(f"globex{uuid.uuid4().hex[:6]}")
    token = create_access_token(member_a, org_id=org_a)
    b_label = await _subdomain_of(org_b)
    async with _client() as client:
        resp = await client.get(
            "/auth/me",
            headers={"X-Chronos-Org": b_label},
            cookies={"chronos_session": token},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Token not valid for this tenant"


@pytest.mark.asyncio
async def test_org_bound_token_rejected_on_no_tenant_host_in_production(monkeypatch):
    """C1: in production, an org-bound token on a host that resolves to no tenant
    (apex / unknown subdomain) is rejected — closing the no-tenant-host hole."""
    org_a, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    token = create_access_token(member_a, org_id=org_a)
    # Force production via the module-level indirection so the no-tenant-host
    # branch rejects. No X-Chronos-Org header → resolved_org_id is None.
    monkeypatch.setattr("core.auth._is_production", lambda: True)
    async with _client() as client:
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Token not valid for this tenant"


@pytest.mark.asyncio
async def test_enforce_flag_rejects_org_less_tokens(monkeypatch):
    """C2: with enforcement on, a legacy org-less token is rejected (401)."""
    _, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    legacy = create_access_token(member_a)  # no org claim
    monkeypatch.setattr("core.auth.settings.enforce_org_bound_tokens", True, raising=False)
    async with _client() as client:
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {legacy}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Session token missing tenant binding"


@pytest.mark.asyncio
async def test_enforce_flag_off_grandfathers_org_less_tokens():
    """C2 default: enforcement off → legacy org-less token still works."""
    _, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    legacy = create_access_token(member_a)
    async with _client() as client:
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {legacy}"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_signup_mints_org_bound_token():
    """A signup token carries the new org's id as its `org` claim."""
    import jwt as _jwt
    from core.config import settings as _settings
    from routers.auth import _otp_store
    import time
    domain = f"flip{uuid.uuid4().hex[:8]}.com"
    email = f"founder@{domain}"
    _otp_store[email] = {"code": "123456", "expires_at": time.time() + 300, "attempts": 0}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        resp = await client.post("/auth/signup", json={"email": email, "code": "123456"})
    assert resp.status_code == 200
    body = resp.json()
    payload = _jwt.decode(body["access_token"], _settings.jwt_secret, algorithms=["HS256"])
    assert payload["org"] == body["org_id"]


@pytest.mark.asyncio
async def test_org_bound_token_accepted_on_no_tenant_host_in_non_production():
    """C1 non-prod arm: an org-bound token on a no-tenant host (no X-Chronos-Org,
    Host 'test', not production) is trusted and accepted — this is what keeps the
    test suite green after the mint flip."""
    org_a, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    token = create_access_token(member_a, org_id=org_a)
    async with _client() as client:
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
