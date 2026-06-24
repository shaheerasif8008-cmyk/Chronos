"""W5.4 — admin console overview.

Proves the aggregated admin summary is admin-gated, tenant-scoped, audited, and
reports real counts.

Requires DATABASE_URL pointing at a migrated Chronos database (defaults to the
local docker Postgres on :55432).
"""
import uuid

import pytest
from sqlalchemy import delete, insert, select

from core.db import engine, reflect_table
from core.exceptions import PermissionDenied
from core.models import Member
from routers import admin


async def _insert_org(org_id: str) -> None:
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(insert(orgs).values(id=org_id, slug=f"slug-{org_id}", name=f"Org {org_id}", plan="pro"))


async def _insert_member(org_id: str, role: str) -> Member:
    mid = f"m-{uuid.uuid4().hex[:8]}"
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(insert(members).values(
            id=mid, organization_id=org_id, email=f"{mid}@example.com", role=role,
        ))
    return Member(id=mid, organization_id=org_id, email=f"{mid}@example.com", role=role)


async def _cleanup(org_ids: list[str]) -> None:
    members = await reflect_table("members")
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(delete(members).where(members.c.organization_id.in_(org_ids)))
        await conn.execute(delete(orgs).where(orgs.c.id.in_(org_ids)))


@pytest.mark.asyncio
async def test_overview_reports_counts_and_is_audited():
    org = f"orgAC-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        owner = await _insert_member(org, "owner")
        await _insert_member(org, "user")
        await _insert_member(org, "user")

        overview = await admin.admin_overview(member=owner)
        assert overview["organization"]["plan"] == "pro"
        assert overview["members"]["total"] == 3
        assert overview["members"]["by_role"]["user"] == 2
        assert "openfga_enabled" in overview["governance"]
        assert overview["governance"]["email_delivery_configured"] is False

        # Viewing the console is audited.
        audit_log = await reflect_table("audit_log")
        async with engine.begin() as conn:
            rows = (await conn.execute(select(audit_log).where(
                audit_log.c.organization_id == org,
                audit_log.c.action == "view_admin_console",
            ))).mappings().all()
        assert rows and rows[0]["actor_id"] == str(owner.id)
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_overview_denied_for_non_admin():
    org = f"orgAC2-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        user = await _insert_member(org, "user")
        with pytest.raises(Exception):  # require_admin raises HTTPException(403)
            await admin.admin_overview(member=user)
    finally:
        await _cleanup([org])
