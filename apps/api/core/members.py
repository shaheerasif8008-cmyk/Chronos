"""Member lookup and provisioning for auth flows."""

from __future__ import annotations

from sqlalchemy import insert, select, update

from core.config import settings
from core.db import engine, reflect_table
from core.models import Member


async def get_member_by_email(email: str) -> Member | None:
    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(members).where(
                    members.c.organization_id == settings.org_id,
                    members.c.email == email.lower(),
                )
            )
        ).mappings().first()
    return Member(**dict(row)) if row else None


# ── Org-aware lookup + provisioning (SSO / SCIM) ──────────────────────────────
# These operate on an explicit org (resolved from the SSO connection or SCIM
# token), not the process default org, so identity flows are tenant-correct.

async def get_member_in_org(org_id: str, *, email: str | None = None, external_id: str | None = None) -> Member | None:
    members = await reflect_table("members")
    clauses = [members.c.organization_id == org_id]
    if external_id is not None:
        clauses.append(members.c.external_id == external_id)
    elif email is not None:
        clauses.append(members.c.email == email.lower())
    else:
        return None
    async with engine.begin() as conn:
        row = (await conn.execute(select(members).where(*clauses))).mappings().first()
    return Member(**dict(row)) if row else None


async def provision_member(
    org_id: str,
    email: str,
    *,
    name: str | None = None,
    role: str = "user",
    external_id: str | None = None,
    sso_subject: str | None = None,
    region: str | None = None,
) -> Member:
    """Find-or-create a member in ``org_id`` and (re)bind external identity.

    Idempotent JIT provisioning: matches on external_id first (stable across email
    changes), then email. Reactivates a previously deactivated member on re-login.
    """
    email = email.lower()
    existing = await get_member_in_org(org_id, external_id=external_id) if external_id else None
    if existing is None:
        existing = await get_member_in_org(org_id, email=email)

    members = await reflect_table("members")
    if existing is not None:
        updates: dict = {"status": "active"}
        if external_id and getattr(existing, "external_id", None) != external_id:
            updates["external_id"] = external_id
        if sso_subject:
            updates["sso_subject"] = sso_subject
        if name:
            updates["name"] = name
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(members).where(members.c.id == existing.id).values(**updates).returning(members)
                )
            ).mappings().one()
        return Member(**dict(row))

    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(members)
                .values(
                    organization_id=org_id,
                    region=region or settings.region,
                    email=email,
                    role=role,
                    name=name or email.split("@", 1)[0],
                    external_id=external_id,
                    sso_subject=sso_subject,
                    status="active",
                )
                .returning(members)
            )
        ).mappings().one()
    return Member(**dict(row))


async def get_member_by_email_global(email: str) -> Member | None:
    """Find a member by email across all orgs (first match by created_at). Used by
    free-email signup to detect a returning user's existing personal org."""
    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(members).where(members.c.email == email.lower()).order_by(members.c.created_at.asc())
        )).mappings().first()
    return Member(**dict(row)) if row else None


async def set_member_status(org_id: str, member_id: str, status: str) -> bool:
    """SCIM lifecycle: 'active' or 'deactivated'. Deactivated members can't log in."""
    members = await reflect_table("members")
    async with engine.begin() as conn:
        result = await conn.execute(
            update(members)
            .where(members.c.organization_id == org_id, members.c.id == member_id)
            .values(status=status)
        )
    return result.rowcount > 0
