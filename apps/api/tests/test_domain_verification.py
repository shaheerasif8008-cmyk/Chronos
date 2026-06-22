"""W1 Phase 3 — DNS-TXT domain verification."""
from __future__ import annotations

import uuid
import pytest

from core import domains
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
