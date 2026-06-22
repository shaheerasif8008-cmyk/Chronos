"""W1 Phase 4 — onboarding state endpoints."""
from __future__ import annotations

import uuid
import httpx
import pytest

import main
from core.auth import create_access_token
from core.db import engine, reflect_table


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _org_and_admin(state: str = "new"):
    org_id = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(
            id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T", onboarding_state=state))
        await conn.execute(members.insert().values(
            id=mid, organization_id=org_id, email=f"{mid[:8]}@t.io", role="admin"))
    return org_id, mid, create_access_token(mid, org_id=org_id), f"o{org_id[:8]}"


@pytest.mark.asyncio
async def test_get_onboarding_state():
    org_id, _, token, sub = await _org_and_admin(state="new")
    async with _client() as client:
        resp = await client.get("/settings/onboarding",
                                headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": sub})
    assert resp.status_code == 200
    assert resp.json()["state"] == "new"


@pytest.mark.asyncio
async def test_complete_onboarding_sets_state_and_persists():
    org_id, _, token, sub = await _org_and_admin(state="new")
    headers = {"Authorization": f"Bearer {token}", "X-Chronos-Org": sub}
    async with _client() as client:
        resp = await client.post("/settings/onboarding/complete", headers=headers)
        assert resp.status_code == 200 and resp.json()["state"] == "complete"
        again = await client.get("/settings/onboarding", headers=headers)
    assert again.json()["state"] == "complete"
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        row = (await conn.execute(orgs.select().where(orgs.c.id == org_id))).mappings().one()
    assert row["onboarding_state"] == "complete"


@pytest.mark.asyncio
async def test_complete_onboarding_rejects_non_admin():
    org_id = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T"))
        await conn.execute(members.insert().values(id=mid, organization_id=org_id, email=f"{mid[:8]}@t.io", role="user"))
    token = create_access_token(mid, org_id=org_id)
    async with _client() as client:
        resp = await client.post("/settings/onboarding/complete",
                                 headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": f"o{org_id[:8]}"})
    assert resp.status_code == 403
