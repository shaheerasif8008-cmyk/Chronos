"""Parameterized org genesis: create + provision a new tenant.

Mirrors the shape of ``seed.py`` (org row + owner member + ``context/{org}/org.md``
+ OpenFGA owner grant), but for any org rather than the seeded ``default`` one.
``ROOT`` matches ``core.context``'s loader root so the context folder is read at
runtime.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import insert

from core import audit, permissions
from core.config import settings
from core.db import engine, reflect_table

# core/provisioning.py -> core -> apps/api (same dir seed.py uses and context loads).
ROOT = Path(__file__).resolve().parent.parent

_ORG_MD_TEMPLATE = "# {name}\n\nWelcome to Chronos. This is your organization's context folder.\n"


async def provision_org(
    *, slug: str, name: str, owner_email: str, owner_name: str | None = None, region: str | None = None
) -> dict:
    """Create a new org with ``slug`` as its (lowercase) subdomain and an owner member.

    Assumes ``slug`` is already validated unique and non-reserved (see core.signup).
    Returns ``{"org_id", "owner_member_id"}``.
    """
    region = region or settings.region
    slug = slug.lower()
    owner_email = owner_email.lower()
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())

    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(insert(organizations).values(
            id=org_id, organization_id=org_id, region=region,
            slug=slug, subdomain=slug, name=name,
            onboarding_state="new", owner_member_id=member_id,
        ))
        await conn.execute(insert(members).values(
            id=member_id, organization_id=org_id, region=region,
            email=owner_email, role="owner",
            name=owner_name or owner_email.split("@", 1)[0],
        ))

    await permissions.grant_org_membership(member_id, org_id, admin=True)

    ctx = ROOT / "context" / org_id
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "org.md").write_text(_ORG_MD_TEMPLATE.format(name=name))

    await audit.log(
        "org_provisioned", member_id, "provisioning.create_org",
        organization_id=org_id, resource_type="organization", resource_id=org_id,
        payload={"slug": slug},
    )
    return {"org_id": org_id, "owner_member_id": member_id}
