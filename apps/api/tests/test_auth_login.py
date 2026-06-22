"""W1 Phase 2B-2 — per-subdomain login resolves the member's own org."""
from __future__ import annotations

import time
import uuid
import httpx
import pytest

import main
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
