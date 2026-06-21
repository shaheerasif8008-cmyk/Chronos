"""W1 Phase 2A — signup helpers + decision logic."""
from __future__ import annotations

import uuid
import pytest

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
