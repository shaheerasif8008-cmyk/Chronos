"""W4 — seat-cap enforcement on invitations."""
from __future__ import annotations

import uuid
import httpx
import pytest

import main
from core.auth import create_access_token
from core.db import engine, reflect_table


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _org_with_members(plan: str, n_members: int):
    org_id = str(uuid.uuid4())
    admin_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(
            id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T", plan=plan))
        await conn.execute(members.insert().values(
            id=admin_id, organization_id=org_id, email=f"admin{admin_id[:8]}@t.io", role="admin"))
        for _ in range(n_members - 1):
            mid = str(uuid.uuid4())
            await conn.execute(members.insert().values(
                id=mid, organization_id=org_id, email=f"{mid[:8]}@t.io", role="viewer"))
    return org_id, create_access_token(admin_id, org_id=org_id), f"o{org_id[:8]}"


@pytest.mark.asyncio
async def test_invite_blocked_at_seat_limit_on_trial():
    # trial max_seats=3; seed 3 members → next invite rejected.
    _, token, sub = await _org_with_members("trial", 3)
    async with _client() as client:
        resp = await client.post("/settings/invitations",
                                 json={"email": f"new{uuid.uuid4().hex[:6]}@t.io", "role": "viewer"},
                                 headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": sub})
    assert resp.status_code == 402


@pytest.mark.asyncio
async def test_invite_allowed_under_seat_limit_on_pro():
    # pro max_seats=25; seed 3 members → invite allowed.
    _, token, sub = await _org_with_members("pro", 3)
    async with _client() as client:
        resp = await client.post("/settings/invitations",
                                 json={"email": f"new{uuid.uuid4().hex[:6]}@t.io", "role": "viewer"},
                                 headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": sub})
    assert resp.status_code == 200
