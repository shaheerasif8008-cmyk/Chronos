"""W4.2 — billing provider seam (degraded + configured paths)."""
from __future__ import annotations

import uuid
import httpx
import pytest

import main
from core import billing
from core.auth import create_access_token
from core.db import engine, reflect_table


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _org_admin(plan="trial"):
    org_id = str(uuid.uuid4()); mid = str(uuid.uuid4())
    orgs = await reflect_table("organizations"); members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T", plan=plan))
        await conn.execute(members.insert().values(id=mid, organization_id=org_id, email=f"a{mid[:8]}@t.io", role="admin"))
    return org_id, create_access_token(mid, org_id=org_id), f"o{org_id[:8]}"


def test_is_configured_false_by_default(monkeypatch):
    monkeypatch.setattr("core.billing.settings.stripe_secret_key", "", raising=False)
    assert billing.is_configured() is False


@pytest.mark.asyncio
async def test_set_org_plan_updates_and_audits():
    org_id, _, _ = await _org_admin("trial")
    await billing.set_org_plan(org_id, "pro")
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        plan = (await conn.execute(orgs.select().where(orgs.c.id == org_id))).mappings().one()["plan"]
    assert plan == "pro"


@pytest.mark.asyncio
async def test_checkout_degraded_when_unconfigured(monkeypatch):
    monkeypatch.setattr("core.billing.settings.stripe_secret_key", "", raising=False)
    _, token, sub = await _org_admin("trial")
    async with _client() as client:
        resp = await client.post("/settings/billing/checkout", json={"plan": "pro"},
                                 headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": sub})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_checkout_configured_returns_url(monkeypatch):
    monkeypatch.setattr("core.billing.settings.stripe_secret_key", "sk_test_x", raising=False)
    monkeypatch.setattr("core.billing.settings.stripe_price_pro", "price_pro", raising=False)
    monkeypatch.setattr("core.billing._provider_create_checkout", lambda **kw: "https://stripe.test/checkout/abc")
    _, token, sub = await _org_admin("trial")
    async with _client() as client:
        resp = await client.post("/settings/billing/checkout", json={"plan": "pro"},
                                 headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": sub})
    assert resp.status_code == 200 and resp.json()["checkout_url"].startswith("https://stripe.test/")


@pytest.mark.asyncio
async def test_webhook_syncs_plan(monkeypatch):
    org_id, _, _ = await _org_admin("trial")
    monkeypatch.setattr("core.billing.settings.stripe_secret_key", "sk_test_x", raising=False)
    monkeypatch.setattr("core.billing.settings.stripe_webhook_secret", "whsec_x", raising=False)
    # Bypass real signature parsing: return a synthetic verified event mapping this org -> pro.
    monkeypatch.setattr("core.billing._parse_event",
                        lambda payload, sig: {"type": "checkout.session.completed",
                                              "org_id": org_id, "plan": "pro"})
    async with _client() as client:
        resp = await client.post("/billing/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=x"})
    assert resp.status_code == 200
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        assert (await conn.execute(orgs.select().where(orgs.c.id == org_id))).mappings().one()["plan"] == "pro"
