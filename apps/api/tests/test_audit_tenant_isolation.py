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
        target_x = await _insert_member(f"tgtx-{uuid.uuid4().hex[:8]}", org_x, "user")
        outsider = await _insert_member(f"out-{uuid.uuid4().hex[:8]}", org_y, "owner")
        target_y = await _insert_member(f"tgty-{uuid.uuid4().hex[:8]}", org_y, "user")

        # Real call sites write an audit entry in each org (resource_id = the
        # target member, so the two entries are distinguishable).
        await settings.update_member_role(
            target_x.id, settings.MemberRolePatch(role="admin"), member=admin
        )
        await settings.update_member_role(
            target_y.id, settings.MemberRolePatch(role="admin"), member=outsider
        )

        # The org-X admin sees their own entry and nothing from org Y.
        as_admin = await settings.list_audit(limit=500, offset=0, member=admin)
        admin_resources = {row["resource_id"] for row in as_admin if row["action"] == "settings.members.role_update"}
        assert target_x.id in admin_resources, (
            "member-triggered audit entry not visible to that member — written under the wrong org"
        )
        assert {row["organization_id"] for row in as_admin} == {org_x}, "org-X feed leaked other orgs"
        assert target_y.id not in admin_resources, "org-Y entry leaked into org-X feed"

        # Positive control + isolation: the org-Y member sees their own entry and
        # not org X's.
        as_outsider = await settings.list_audit(limit=500, offset=0, member=outsider)
        outsider_resources = {row["resource_id"] for row in as_outsider if row["action"] == "settings.members.role_update"}
        assert target_y.id in outsider_resources, "org-Y member cannot read their own audit feed"
        assert {row["organization_id"] for row in as_outsider} == {org_y}, "org-Y feed leaked other orgs"
        assert target_x.id not in outsider_resources, "org-X entry leaked into org-Y feed"
    finally:
        await _cleanup([org_x, org_y])
