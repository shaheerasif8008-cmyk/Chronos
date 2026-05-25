"""Member lookup and provisioning for auth flows."""

from __future__ import annotations

from sqlalchemy import insert, select

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


async def get_or_create_member_for_email(email: str, *, name: str | None = None) -> Member:
    existing = await get_member_by_email(email)
    if existing:
        return existing
    if not settings.cognito_auto_provision_members:
        raise PermissionError("Email is not registered as a Chronos member")

    members = await reflect_table("members")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(members)
            .values(
                organization_id=settings.org_id,
                region=settings.region,
                email=email.lower(),
                role="user",
                name=name or email.split("@", 1)[0],
            )
            .returning(members)
        )
        row = result.mappings().one()
    return Member(**dict(row))
