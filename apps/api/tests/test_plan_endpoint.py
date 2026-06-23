"""W4 — GET /settings/plan."""
from __future__ import annotations

import uuid
import httpx
import pytest

import main
from core.auth import create_access_token
from core.db import engine, reflect_table


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _org_admin(plan: str, n_members: int = 2):
    org_id = str(uuid.uuid4())
    admin_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(
            id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T", plan=plan))
        await conn.execute(members.insert().values(
            id=admin_id, organization_id=org_id, email=f"a{admin_id[:8]}@t.io", role="admin"))
        for _ in range(n_members - 1):
            mid = str(uuid.uuid4())
            await conn.execute(members.insert().values(
                id=mid, organization_id=org_id, email=f"{mid[:8]}@t.io", role="user"))
    return create_access_token(admin_id, org_id=org_id), f"o{org_id[:8]}"


@pytest.mark.asyncio
async def test_get_plan_returns_entitlements_and_usage():
    token, sub = await _org_admin("pro", n_members=2)
    async with _client() as client:
        resp = await client.get("/settings/plan",
                                headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": sub})
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"] == "pro"
    assert body["entitlements"]["max_seats"] == 25
    assert body["usage"]["seats_used"] == 2


@pytest.mark.asyncio
async def test_get_plan_includes_features_as_sorted_list():
    token, sub = await _org_admin("pro", n_members=1)
    async with _client() as client:
        resp = await client.get("/settings/plan",
                                headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": sub})
    assert resp.status_code == 200
    body = resp.json()
    features = body["entitlements"]["features"]
    assert isinstance(features, list)
    assert features == sorted(features)
    assert "sso" in features


@pytest.mark.asyncio
async def test_get_plan_trial_returns_trial_entitlements():
    token, sub = await _org_admin("trial", n_members=1)
    async with _client() as client:
        resp = await client.get("/settings/plan",
                                headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": sub})
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"] == "trial"
    assert body["entitlements"]["max_seats"] == 3
    assert body["entitlements"]["daily_cost_limit_usd"] == 5.0
    assert body["entitlements"]["daily_token_limit"] == 200_000


@pytest.mark.asyncio
async def test_get_plan_requires_auth():
    async with _client() as client:
        resp = await client.get("/settings/plan")
    assert resp.status_code in (401, 403)
