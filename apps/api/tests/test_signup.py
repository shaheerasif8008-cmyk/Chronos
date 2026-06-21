"""W1 Phase 2A — signup helpers + decision logic."""
from __future__ import annotations

import time
import uuid
import pytest
import httpx
import main
from routers.auth import _otp_store

from core.signup import RESERVED_SLUGS, derive_slug, is_free_email_domain, signup_or_join, unique_subdomain
from core.db import engine, reflect_table


@pytest.mark.parametrize("domain,expected", [
    ("gmail.com", True), ("outlook.com", True), ("yahoo.com", True), ("icloud.com", True),
    ("novatech.com", False), ("acme.io", False),
])
def test_is_free_email_domain(domain, expected):
    assert is_free_email_domain(domain) is expected


@pytest.mark.parametrize("raw,expected", [
    ("NovaTech", "novatech"),
    ("nova tech!!", "nova-tech"),
    ("a..b__c", "a-b-c"),
    ("---x---", "x"),
    ("", "org"),
])
def test_derive_slug(raw, expected):
    assert derive_slug(raw) == expected


def test_reserved_slugs_include_resolution_blocklist():
    for s in ("app", "www", "api", "admin", "default"):
        assert s in RESERVED_SLUGS


@pytest.mark.asyncio
async def test_unique_subdomain_suffixes_on_collision_and_reserved():
    base = f"taken{uuid.uuid4().hex[:8]}"
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(
            id=str(uuid.uuid4()), slug=base, subdomain=base, name="T",
        ))
    out = await unique_subdomain(base)
    assert out != base and out.startswith(base)
    reserved_out = await unique_subdomain("api")
    assert reserved_out not in RESERVED_SLUGS


def _domain() -> str:
    return f"co{uuid.uuid4().hex[:8]}.com"


@pytest.mark.asyncio
async def test_unclaimed_work_domain_creates_org_and_claims():
    domain = _domain()
    email = f"founder@{domain}"
    res = await signup_or_join(email, org_name="Acme")
    assert res["created"] is True and res["role"] == "owner" and res["member_id"]
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        claim = (await conn.execute(claims.select().where(claims.c.domain == domain))).mappings().one()
    assert claim["organization_id"] == res["org_id"]
    assert claim["claim_type"] == "soft_email" and claim["join_policy"] == "auto"


@pytest.mark.asyncio
async def test_second_same_domain_signup_auto_joins_as_user():
    domain = _domain()
    first = await signup_or_join(f"founder@{domain}")
    second = await signup_or_join(f"teammate@{domain}")
    assert second["created"] is False and second["joined"] is True
    assert second["org_id"] == first["org_id"] and second["role"] == "user"
    assert second["member_id"] != first["member_id"]


@pytest.mark.asyncio
async def test_existing_member_signup_is_login_not_duplicate():
    domain = _domain()
    first = await signup_or_join(f"founder@{domain}")
    again = await signup_or_join(f"founder@{domain}")
    assert again["org_id"] == first["org_id"]
    assert again["member_id"] == first["member_id"]
    assert again["created"] is False and again["joined"] is False


@pytest.mark.asyncio
async def test_free_email_creates_personal_org_without_claim():
    email = f"person{uuid.uuid4().hex[:8]}@gmail.com"
    res = await signup_or_join(email)
    assert res["created"] is True and res["role"] == "owner"
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        rows = (await conn.execute(claims.select().where(claims.c.domain == "gmail.com"))).all()
    assert rows == []


@pytest.mark.asyncio
async def test_claimed_domain_with_approval_policy_creates_pending_member():
    domain = _domain()
    # Create an org via signup, then flip its claim to approval policy.
    first = await signup_or_join(f"founder@{domain}")
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        await conn.execute(
            claims.update().where(claims.c.domain == domain).values(join_policy="approval")
        )
    res = await signup_or_join(f"applicant@{domain}")
    assert res.get("status") == "pending_approval"
    assert res["member_id"] is None and res["org_id"] == first["org_id"]
    # A pending member row exists in that org.
    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (await conn.execute(
            members.select().where(
                members.c.organization_id == first["org_id"],
                members.c.email == f"applicant@{domain}",
            )
        )).mappings().one()
    assert row["status"] == "pending_approval"


@pytest.mark.asyncio
async def test_signup_normalizes_email_case_to_same_org():
    domain = _domain()
    first = await signup_or_join(f"Founder@{domain}")
    again = await signup_or_join(f"FOUNDER@{domain.upper()}")
    assert again["org_id"] == first["org_id"]
    assert again["member_id"] == first["member_id"]
    assert again["created"] is False


# ---------------------------------------------------------------------------
# HTTP endpoint tests — POST /auth/signup
# ---------------------------------------------------------------------------

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


def _seed_otp(email: str, code: str = "123456") -> None:
    _otp_store[email.lower()] = {"code": code, "expires_at": time.time() + 300, "attempts": 0}


@pytest.mark.asyncio
async def test_signup_endpoint_creates_org_for_unclaimed_domain():
    domain = _domain()
    email = f"founder@{domain}"
    _seed_otp(email)
    async with _client() as client:
        resp = await client.post("/auth/signup", json={"email": email, "code": "123456", "org_name": "Acme"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True and body["org_id"] and body["member_id"]
    assert body["access_token"]


@pytest.mark.asyncio
async def test_signup_endpoint_rejects_bad_otp_and_creates_nothing():
    domain = _domain()
    email = f"founder@{domain}"
    _seed_otp(email, code="111111")
    async with _client() as client:
        resp = await client.post("/auth/signup", json={"email": email, "code": "999999"})
    assert resp.status_code == 400
    claims = await reflect_table("email_domain_claims")
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        claim_rows = (await conn.execute(claims.select().where(claims.c.domain == domain))).all()
        # the derived subdomain for this domain must not exist either
        sub = domain.split(".", 1)[0]
        org_rows = (await conn.execute(orgs.select().where(orgs.c.subdomain == sub))).all()
    assert claim_rows == [] and org_rows == []


@pytest.mark.asyncio
async def test_signup_endpoint_second_same_domain_auto_joins():
    domain = _domain()
    _seed_otp(f"founder@{domain}")
    async with _client() as client:
        await client.post("/auth/signup", json={"email": f"founder@{domain}", "code": "123456"})
        _seed_otp(f"teammate@{domain}")
        resp = await client.post("/auth/signup", json={"email": f"teammate@{domain}", "code": "123456"})
    assert resp.status_code == 200
    assert resp.json()["created"] is False


@pytest.mark.asyncio
async def test_signup_endpoint_pending_approval_returns_no_token():
    domain = _domain()
    _seed_otp(f"founder@{domain}")
    async with _client() as client:
        await client.post("/auth/signup", json={"email": f"founder@{domain}", "code": "123456"})
    # Flip the domain claim to approval policy.
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        await conn.execute(claims.update().where(claims.c.domain == domain).values(join_policy="approval"))
    _seed_otp(f"applicant@{domain}")
    async with _client() as client:
        resp = await client.post("/auth/signup", json={"email": f"applicant@{domain}", "code": "123456"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_approval" and "access_token" not in body


@pytest.mark.asyncio
async def test_signup_endpoint_404_when_dev_otp_disabled(monkeypatch):
    monkeypatch.setattr("routers.auth._dev_otp_enabled", lambda: False)
    async with _client() as client:
        resp = await client.post("/auth/signup", json={"email": f"x@{_domain()}", "code": "123456"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_free_email_repeat_signup_is_idempotent():
    email = f"person{uuid.uuid4().hex[:8]}@gmail.com"
    first = await signup_or_join(email)
    again = await signup_or_join(email)
    assert again["org_id"] == first["org_id"]
    assert again["member_id"] == first["member_id"]
    assert again["created"] is False


@pytest.mark.asyncio
async def test_unclaimed_domain_race_resolves_to_join(monkeypatch):
    """If the domain gets claimed between our check and our insert (concurrent
    signup), the loser re-resolves to an auto-join instead of 500ing/orphaning."""
    import core.signup as signup_mod
    domain = f"race{uuid.uuid4().hex[:8]}.com"
    winner = await signup_or_join(f"founder@{domain}")
    real = signup_mod._claim_for_domain
    calls = {"n": 0}
    async def fake_claim(d):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # pretend unclaimed → take the create-org branch
        return await real(d)
    monkeypatch.setattr(signup_mod, "_claim_for_domain", fake_claim)
    res = await signup_or_join(f"loser@{domain}")
    assert res["org_id"] == winner["org_id"]
    assert res["created"] is False and res["joined"] is True
