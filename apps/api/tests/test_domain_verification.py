"""W1 Phase 3 — DNS-TXT domain verification."""
from __future__ import annotations

import uuid
import pytest
import httpx
import main

from core import domains
from core.auth import create_access_token
from core.db import engine, reflect_table


async def _seed_claim(domain: str) -> str:
    org_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T"))
        await conn.execute(claims.insert().values(
            organization_id=org_id, domain=domain, claim_type="soft_email", join_policy="auto"))
    return org_id


@pytest.mark.asyncio
async def test_start_returns_txt_record_and_persists_pending():
    domain = f"verify{uuid.uuid4().hex[:8]}.com"
    org_id = await _seed_claim(domain)
    rec = await domains.start_domain_verification(org_id, domain)
    assert rec["name"].endswith(domain) and rec["value"].startswith("chronos-verify=")
    verifs = await reflect_table("domain_verifications")
    async with engine.begin() as conn:
        row = (await conn.execute(verifs.select().where(
            verifs.c.organization_id == org_id, verifs.c.domain == domain))).mappings().one()
    assert row["status"] == "pending" and row["txt_token"] in rec["value"]


@pytest.mark.asyncio
async def test_check_verifies_and_upgrades_claim_when_txt_present(monkeypatch):
    domain = f"verify{uuid.uuid4().hex[:8]}.com"
    org_id = await _seed_claim(domain)
    rec = await domains.start_domain_verification(org_id, domain)
    token = rec["value"].split("=", 1)[1]
    monkeypatch.setattr(domains, "_lookup_txt", lambda d: [f"chronos-verify={token}", "v=spf1 -all"])
    ok = await domains.check_domain_verification(org_id, domain)
    assert ok is True
    assert await domains.is_domain_hard_claimed(org_id, domain) is True
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        ct = (await conn.execute(claims.select().where(
            claims.c.organization_id == org_id, claims.c.domain == domain))).mappings().one()["claim_type"]
    assert ct == "verified_dns"


@pytest.mark.asyncio
async def test_check_fails_when_txt_absent(monkeypatch):
    domain = f"verify{uuid.uuid4().hex[:8]}.com"
    org_id = await _seed_claim(domain)
    await domains.start_domain_verification(org_id, domain)
    monkeypatch.setattr(domains, "_lookup_txt", lambda d: ["unrelated-record"])
    assert await domains.check_domain_verification(org_id, domain) is False
    assert await domains.is_domain_hard_claimed(org_id, domain) is False


# ── Task 3: HTTP endpoints ────────────────────────────────────────────────────

def _http_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _admin_in_org(org_id: str) -> str:
    members = await reflect_table("members")
    mid = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(members.insert().values(
            id=mid, organization_id=org_id, email=f"{mid[:8]}@t.io", role="admin"))
    return create_access_token(mid, org_id=org_id)


async def _subdomain(org_id: str) -> str:
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        return (await conn.execute(orgs.select().where(orgs.c.id == org_id))).mappings().one()["subdomain"]


@pytest.mark.asyncio
async def test_verify_start_then_check_via_http(monkeypatch):
    domain = f"verify{uuid.uuid4().hex[:8]}.com"
    org_id = await _seed_claim(domain)
    token = await _admin_in_org(org_id)
    headers = {"Authorization": f"Bearer {token}", "X-Chronos-Org": await _subdomain(org_id)}
    async with _http_client() as client:
        start = await client.post("/domains/verify/start", json={"domain": domain}, headers=headers)
        assert start.status_code == 200
        tok = start.json()["record"]["value"].split("=", 1)[1]
        monkeypatch.setattr("core.domains._lookup_txt", lambda d: [f"chronos-verify={tok}"])
        check = await client.post("/domains/verify/check", json={"domain": domain}, headers=headers)
        assert check.status_code == 200 and check.json()["verified"] is True


@pytest.mark.asyncio
async def test_verify_check_fails_without_txt(monkeypatch):
    domain = f"verify{uuid.uuid4().hex[:8]}.com"
    org_id = await _seed_claim(domain)
    token = await _admin_in_org(org_id)
    headers = {"Authorization": f"Bearer {token}", "X-Chronos-Org": await _subdomain(org_id)}
    async with _http_client() as client:
        await client.post("/domains/verify/start", json={"domain": domain}, headers=headers)
        monkeypatch.setattr("core.domains._lookup_txt", lambda d: ["nope"])
        check = await client.post("/domains/verify/check", json={"domain": domain}, headers=headers)
        assert check.status_code == 400


# ── Task 4: SSO connections require a hard-claimed domain ─────────────────────

@pytest.mark.asyncio
async def test_sso_connection_requires_hard_claimed_domain(monkeypatch):
    domain = f"ssov{uuid.uuid4().hex[:8]}.com"
    org_id = await _seed_claim(domain)  # soft claim only
    token = await _admin_in_org(org_id)
    headers = {"Authorization": f"Bearer {token}", "X-Chronos-Org": await _subdomain(org_id)}
    body = {
        "issuer": "https://idp.example.com",
        "client_id": "abc",
        "client_secret": "s",
        "email_domain": domain,
    }
    async with _http_client() as client:
        r1 = await client.post("/auth/sso/connections", json=body, headers=headers)
        assert r1.status_code == 403  # soft claim → rejected
        rec = await domains.start_domain_verification(org_id, domain)
        tok = rec["value"].split("=", 1)[1]
        monkeypatch.setattr("core.domains._lookup_txt", lambda d: [f"chronos-verify={tok}"])
        await domains.check_domain_verification(org_id, domain)
        r2 = await client.post("/auth/sso/connections", json=body, headers=headers)
        assert r2.status_code == 200  # now hard-claimed → allowed
