"""W4.3 — persistent monthly usage metering."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
import httpx
import pytest

import main
from core import billing_usage
from core.auth import create_access_token
from core.db import engine, reflect_table


def _period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def _org_admin(plan="pro"):
    org_id = str(uuid.uuid4()); mid = str(uuid.uuid4())
    orgs = await reflect_table("organizations"); members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T", plan=plan))
        await conn.execute(members.insert().values(id=mid, organization_id=org_id, email=f"a{mid[:8]}@t.io", role="admin"))
    return org_id, create_access_token(mid, org_id=org_id), f"o{org_id[:8]}"


@pytest.mark.asyncio
async def test_record_usage_accumulates_in_period():
    org_id, _, _ = await _org_admin()
    await billing_usage.record(org_id, tokens=100, cost_usd=0.5)
    await billing_usage.record(org_id, tokens=50, cost_usd=0.25)
    usage = await billing_usage.monthly_usage(org_id)
    assert usage["period"] == _period()
    assert usage["tokens"] == 150
    assert abs(usage["cost_usd"] - 0.75) < 1e-9


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


@pytest.mark.asyncio
async def test_billing_usage_endpoint():
    org_id, token, sub = await _org_admin("pro")
    await billing_usage.record(org_id, tokens=1000, cost_usd=2.0)
    async with _client() as client:
        resp = await client.get("/settings/billing/usage",
                                headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": sub})
    assert resp.status_code == 200
    body = resp.json()
    assert body["period"] == _period()
    assert body["tokens"] == 1000
    assert "entitlements" in body and "over_budget" in body
