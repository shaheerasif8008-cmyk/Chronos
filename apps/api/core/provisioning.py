"""Parameterized org genesis: create + provision a new tenant.

Mirrors the shape of ``seed.py`` (org row + owner member + ``context/{org}/org.md``
+ OpenFGA owner grant), but for any org rather than the seeded ``default`` one.
``ROOT`` is imported from ``core.context`` so the context folder is always read
from the same location at runtime.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import insert

from core import audit, permissions
from core.config import settings
from core.context import ROOT  # same root core.context.load_org_context reads from
from core.db import engine, reflect_table

logger = logging.getLogger(__name__)

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
    workspaces = await reflect_table("workspaces")
    workspace_members = await reflect_table("workspace_members")
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
        workspace_id = (
            await conn.execute(
                insert(workspaces)
                .values(
                    organization_id=org_id,
                    region=region,
                    name="Default workspace",
                    slug="default",
                    legacy_key="default",
                    status="active",
                    created_by=member_id,
                )
                .returning(workspaces.c.id)
            )
        ).scalar_one()
        await conn.execute(
            insert(workspace_members).values(
                organization_id=org_id,
                region=region,
                workspace_id=str(workspace_id),
                member_id=member_id,
                role="owner",
                added_by=member_id,
            )
        )

    # Best-effort: the org+owner are already durably committed. A grant failure
    # (OpenFGA configured but unreachable) must not lose the signup; the tuple is
    # idempotently re-grantable. Surface it for observability instead of 500ing.
    try:
        await permissions.grant_org_membership(member_id, org_id, admin=True)
        await permissions.grant_workspace_role(
            member_id, "owner", str(workspace_id), org_id
        )
    except Exception:
        logger.warning(
            "OpenFGA owner/workspace grant failed for org %s; re-grant needed",
            org_id,
            exc_info=True,
        )

    ctx = ROOT / "context" / org_id
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "org.md").write_text(_ORG_MD_TEMPLATE.format(name=name))

    await audit.log(
        "org_provisioned", member_id, "provisioning.create_org",
        organization_id=org_id, resource_type="organization", resource_id=org_id,
        payload={"slug": slug},
    )
    return {"org_id": org_id, "owner_member_id": member_id}
