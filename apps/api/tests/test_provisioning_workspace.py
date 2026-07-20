from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from core import provisioning
from core.db import engine, reflect_table
from core.models import Member
from core.signup import signup_or_join
from core.workspace_access import require_workspace_access


@pytest.mark.asyncio
async def test_provision_org_creates_writable_default_workspace(monkeypatch):
    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(provisioning.permissions, "grant_org_membership", no_op)
    monkeypatch.setattr(provisioning.permissions, "grant_workspace_role", no_op)
    monkeypatch.setattr(provisioning.audit, "log", no_op)

    suffix = uuid.uuid4().hex[:10]
    result = await provisioning.provision_org(
        slug=f"eval-{suffix}",
        name="Synthetic evaluation tenant",
        owner_email=f"owner@eval-{suffix}.dev",
    )

    workspaces = await reflect_table("workspaces")
    memberships = await reflect_table("workspace_members")
    async with engine.begin() as conn:
        workspace = (
            await conn.execute(
                select(workspaces).where(
                    workspaces.c.organization_id == result["org_id"],
                    workspaces.c.legacy_key == "default",
                )
            )
        ).mappings().one()
        membership = (
            await conn.execute(
                select(memberships).where(
                    memberships.c.organization_id == result["org_id"],
                    memberships.c.workspace_id == workspace["id"],
                    memberships.c.member_id == result["owner_member_id"],
                )
            )
        ).mappings().one()

    assert workspace["status"] == "active"
    assert membership["role"] == "owner"

    writable = await require_workspace_access(
        Member(
            id=result["owner_member_id"],
            organization_id=result["org_id"],
            email=f"owner@eval-{suffix}.dev",
            role="owner",
        ),
        "default",
        access="write",
    )
    assert writable["id"] == workspace["id"]


@pytest.mark.asyncio
async def test_repeated_signup_reuses_one_default_workspace(monkeypatch):
    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(provisioning.permissions, "grant_org_membership", no_op)
    monkeypatch.setattr(provisioning.permissions, "grant_workspace_role", no_op)
    monkeypatch.setattr(provisioning.audit, "log", no_op)

    suffix = uuid.uuid4().hex[:10]
    email = f"owner@repeat-{suffix}.dev"
    first = await signup_or_join(email, org_name="Repeated synthetic tenant")
    second = await signup_or_join(email, org_name="Repeated synthetic tenant")

    workspaces = await reflect_table("workspaces")
    memberships = await reflect_table("workspace_members")
    async with engine.begin() as conn:
        workspace_rows = (
            await conn.execute(
                select(workspaces).where(
                    workspaces.c.organization_id == first["org_id"],
                    workspaces.c.legacy_key == "default",
                )
            )
        ).mappings().all()
        membership_rows = (
            await conn.execute(
                select(memberships).where(
                    memberships.c.organization_id == first["org_id"],
                    memberships.c.member_id == first["member_id"],
                )
            )
        ).mappings().all()

    assert second["org_id"] == first["org_id"]
    assert second["member_id"] == first["member_id"]
    assert len(workspace_rows) == 1
    assert len(membership_rows) == 1
