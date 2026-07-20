"""W1 Phase 3 — DNS-TXT domain verification."""
from __future__ import annotations

import uuid
import pytest
import httpx
import main

from core import domains, sso
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
async def test_check_returns_false_when_no_claim_to_upgrade(monkeypatch):
    """DNS matches but the org holds no claim on the domain → not 'verified'."""
    domain = f"noclaim{uuid.uuid4().hex[:8]}.com"
    # Create an org WITHOUT an email_domain_claims row for this domain.
    org_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T"))
    rec = await domains.start_domain_verification(org_id, domain)
    tok = rec["value"].split("=", 1)[1]
    monkeypatch.setattr(domains, "_lookup_txt", lambda d: [f"chronos-verify={tok}"])
    assert await domains.check_domain_verification(org_id, domain) is False
    assert await domains.is_domain_hard_claimed(org_id, domain) is False


@pytest.mark.asyncio
async def test_verify_start_rejects_non_admin():
    domain = f"verify{uuid.uuid4().hex[:8]}.com"
    org_id = await _seed_claim(domain)
    # A non-admin member of the org.
    members = await reflect_table("members")
    mid = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(members.insert().values(
            id=mid, organization_id=org_id, email=f"{mid[:8]}@t.io", role="user"))
    token = create_access_token(mid, org_id=org_id)
    headers = {"Authorization": f"Bearer {token}", "X-Chronos-Org": await _subdomain(org_id)}
    async with _http_client() as client:
        resp = await client.post("/domains/verify/start", json={"domain": domain}, headers=headers)
    assert resp.status_code == 403


# ── Task 4: SSO connections require a hard-claimed domain ─────────────────────

@pytest.mark.asyncio
async def test_sso_connection_requires_hard_claimed_domain(monkeypatch):
    # Exercise the production storage path from the initial create. Production
    # cannot boot without this key; leaving it blank here would intentionally
    # select the development-only plaintext compatibility path.
    monkeypatch.setattr(sso.settings, "vault_encryption_key", "22" * 32)
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
        connection_id = r2.json()["id"]
        monkeypatch.setattr("core.sso.assert_safe_url", lambda url: url)
        async def _discovery(_issuer: str) -> dict[str, str]:
            return {
                "authorize_url": "https://idp.example.com/authorize",
                "token_url": "https://idp.example.com/token",
                "jwks_url": "https://idp.example.com/jwks",
                "userinfo_url": "",
            }
        monkeypatch.setattr("core.sso.discover", _discovery)
        started = await client.get(
            "/auth/sso/start", params={"email": f"member@{domain}"}
        )
        assert started.status_code == 200
        assert "chronos_sso_state=" in started.headers.get("set-cookie", "")
        patched = await client.patch(
            f"/auth/sso/connections/{connection_id}",
            json={"enabled": False, "client_secret": ""},
            headers=headers,
        )
        assert patched.status_code == 200
        assert patched.json()["has_client_secret"] is True

        # Empty is preserve-only; a non-empty PATCH performs an encrypted
        # rotation when the production vault key is configured.
        table = await reflect_table("sso_connections")
        async with engine.begin() as conn:
            preserved = (
                await conn.execute(table.select().where(table.c.id == connection_id))
            ).mappings().one()["client_secret"]
        assert preserved.startswith("enc:v1:")
        assert sso.reveal_client_secret(
            preserved, organization_id=org_id
        ) == "s"
        rotated = await client.patch(
            f"/auth/sso/connections/{connection_id}",
            json={"client_secret": "replacement-secret"},
            headers=headers,
        )
        assert rotated.status_code == 200

    # The replacement is encrypted at rest and decrypts only in the owning org.
    async with engine.begin() as conn:
        stored = (
            await conn.execute(table.select().where(table.c.id == connection_id))
        ).mappings().one()
    assert stored["client_secret"].startswith("enc:v1:")
    assert sso.reveal_client_secret(
        stored["client_secret"], organization_id=org_id
    ) == "replacement-secret"
    assert stored["enabled"] is False
