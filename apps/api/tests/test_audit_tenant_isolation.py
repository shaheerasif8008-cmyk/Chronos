"""Audit log must be written under the actor's org, not the process default.

Regression for the tenant-isolation bug where ``audit.log`` always wrote
``organization_id = settings.org_id`` while ``/settings/audit`` reads filter by
the member's own ``organization_id``. In a deployment where a member's org is
not the process default, their audit entries landed under the wrong org and
became invisible to them.

Requires DATABASE_URL pointing at a migrated Chronos database (defaults to the
local docker Postgres on :55432).
"""
import uuid

import pytest

from sqlalchemy import delete, insert

from core.db import engine, reflect_table
from core.models import Member
from routers import settings


async def _insert_org(org_id: str) -> None:
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(
            insert(orgs).values(id=org_id, slug=f"slug-{org_id}", name=f"Org {org_id}")
        )


async def _insert_member(member_id: str, org_id: str, role: str) -> Member:
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(
            insert(members).values(
                id=member_id,
                organization_id=org_id,
                email=f"{member_id}@example.com",
                role=role,
            )
        )
    return Member(id=member_id, organization_id=org_id, email=f"{member_id}@example.com", role=role)


async def _cleanup(org_ids: list[str]) -> None:
    # audit_log is append-only (DELETE is blocked by trigger), so test rows there
    # are isolated by their unique org id instead of being removed.
    members = await reflect_table("members")
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(delete(members).where(members.c.organization_id.in_(org_ids)))
        await conn.execute(delete(orgs).where(orgs.c.id.in_(org_ids)))


@pytest.mark.asyncio
async def test_member_triggered_audit_entry_is_visible_to_that_member_and_isolated():
    org_x = f"orgX-{uuid.uuid4().hex[:8]}"
    org_y = f"orgY-{uuid.uuid4().hex[:8]}"
    await _insert_org(org_x)
    await _insert_org(org_y)
    try:
        admin = await _insert_member(f"admin-{uuid.uuid4().hex[:8]}", org_x, "owner")
        target = await _insert_member(f"target-{uuid.uuid4().hex[:8]}", org_x, "user")
        outsider = await _insert_member(f"out-{uuid.uuid4().hex[:8]}", org_y, "owner")

        # Real call site writes an audit entry on behalf of the org-X admin.
        await settings.update_member_role(
            target.id, settings.MemberRolePatch(role="admin"), member=admin
        )

        # The triggering member (org X) can read their own audit entry.
        as_admin = await settings.list_audit(limit=500, offset=0, member=admin)
        actions = {row["action"] for row in as_admin}
        assert "settings.members.role_update" in actions, (
            "member-triggered audit entry not visible to that member — "
            "written under the wrong org"
        )
        org_ids = {row["organization_id"] for row in as_admin}
        assert org_ids == {org_x}, f"audit rows leaked from other orgs: {org_ids}"

        # A member in a different org must not see org X's audit entry.
        as_outsider = await settings.list_audit(limit=500, offset=0, member=outsider)
        assert all(row["organization_id"] == org_y for row in as_outsider)
        assert "settings.members.role_update" not in {row["action"] for row in as_outsider}
    finally:
        await _cleanup([org_x, org_y])
