"""Self-serve signup: domain classification, slug derivation, and the
create-org / join-org decision for a verified email."""
from __future__ import annotations

import re
import secrets
import uuid as _uuid

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.members import get_member_by_email_global, get_member_in_org, provision_member
from core.provisioning import provision_org
from core.tenancy import RESERVED_LABELS

FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "ymail.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com",
    "yandex.com", "zoho.com", "qq.com", "163.com",
})

RESERVED_SLUGS = RESERVED_LABELS | frozenset({
    "default", "signup", "login", "onboarding", "help", "support", "status",
    "docs", "blog", "mail", "dashboard", "account", "billing",
})


def is_free_email_domain(domain: str) -> bool:
    return domain.lower().strip() in FREE_EMAIL_DOMAINS


def derive_slug(base: str) -> str:
    """Lowercase, collapse non [a-z0-9-] to single hyphens, trim. Empty -> 'org'."""
    s = re.sub(r"[^a-z0-9-]+", "-", base.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "org"


async def unique_subdomain(candidate: str) -> str:
    """Return an available, non-reserved subdomain derived from ``candidate``."""
    label = derive_slug(candidate)
    if label in RESERVED_SLUGS:
        label = f"{label}-org"
    organizations = await reflect_table("organizations")

    async def _taken(value: str) -> bool:
        async with engine.begin() as conn:
            return (await conn.execute(
                select(organizations.c.id).where(organizations.c.subdomain == value)
            )).first() is not None

    if not await _taken(label):
        return label
    while True:
        suffixed = f"{label}-{secrets.token_hex(2)}"
        if suffixed not in RESERVED_SLUGS and not await _taken(suffixed):
            return suffixed


async def _claim_for_domain(domain: str) -> dict | None:
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(claims).where(claims.c.domain == domain)
        )).mappings().first()
    return dict(row) if row else None


async def signup_or_join(email: str, org_name: str | None = None) -> dict:
    """Turn a verified ``email`` into an org membership.

    Returns: ``org_id``, ``member_id`` (None if pending approval), ``role``,
    ``created`` (new org), ``joined`` (joined existing org), optionally
    ``status`` == "pending_approval".

    On the approval path a members row IS written with status='pending_approval';
    member_id is intentionally omitted from the response until an admin approves.
    """
    email = email.lower().strip()
    domain = email.split("@", 1)[1]

    if is_free_email_domain(domain):
        existing = await get_member_by_email_global(email)
        if existing is not None:
            return {"org_id": str(existing.organization_id), "member_id": str(existing.id),
                    "role": existing.role, "created": False, "joined": False}
        local = email.split("@", 1)[0]
        sub = await unique_subdomain(local)
        prov = await provision_org(slug=sub, name=org_name or f"{local}'s workspace", owner_email=email)
        return {"org_id": prov["org_id"], "member_id": prov["owner_member_id"],
                "role": "owner", "created": True, "joined": False}

    claim = await _claim_for_domain(domain)
    if claim is not None:
        org_id = claim["organization_id"]
        existing = await get_member_in_org(org_id, email=email)
        if existing is not None:
            return {"org_id": org_id, "member_id": str(existing.id), "role": existing.role,
                    "created": False, "joined": False}
        if claim["join_policy"] == "approval":
            # Direct insert (not provision_member): provision_member hardcodes
            # status="active"; a pending member must be created as pending_approval.
            members = await reflect_table("members")
            member_id = str(_uuid.uuid4())
            async with engine.begin() as conn:
                await conn.execute(insert(members).values(
                    id=member_id, organization_id=org_id, region=settings.region,
                    email=email, role="user", name=email.split("@", 1)[0],
                    status="pending_approval",
                ))
            await audit.log("signup_pending_approval", member_id, "signup.join",
                            organization_id=org_id, payload={"domain": domain})
            return {"org_id": org_id, "member_id": None, "role": "user",
                    "created": False, "joined": False, "status": "pending_approval"}
        member = await provision_member(org_id, email, role="user")
        return {"org_id": org_id, "member_id": str(member.id), "role": "user",
                "created": False, "joined": True}

    # Unclaimed work domain: create the org, make the signer owner, soft-claim.
    sub = await unique_subdomain(domain.split(".", 1)[0])
    prov = await provision_org(slug=sub, name=org_name or domain.split(".", 1)[0].title(), owner_email=email)
    claims = await reflect_table("email_domain_claims")
    try:
        async with engine.begin() as conn:
            await conn.execute(insert(claims).values(
                organization_id=prov["org_id"], region=settings.region, domain=domain,
                claim_type="soft_email", join_policy="auto",
            ))
    except IntegrityError:
        # Lost the race: a concurrent signup claimed this domain first. Roll back
        # the org we just created, then re-resolve through the normal path (which
        # now finds the claim and handles auto-join vs. approval correctly).
        orgs = await reflect_table("organizations")
        members = await reflect_table("members")
        workspaces = await reflect_table("workspaces")
        workspace_members = await reflect_table("workspace_members")
        async with engine.begin() as conn:
            await conn.execute(
                workspace_members.delete().where(
                    workspace_members.c.organization_id == prov["org_id"]
                )
            )
            await conn.execute(
                workspaces.delete().where(
                    workspaces.c.organization_id == prov["org_id"]
                )
            )
            await conn.execute(members.delete().where(members.c.id == prov["owner_member_id"]))
            await conn.execute(orgs.delete().where(orgs.c.id == prov["org_id"]))
        return await signup_or_join(email, org_name)
    await audit.log("domain_soft_claimed", prov["owner_member_id"], "signup.claim_domain",
                    organization_id=prov["org_id"], payload={"domain": domain})
    return {"org_id": prov["org_id"], "member_id": prov["owner_member_id"],
            "role": "owner", "created": True, "joined": False}
