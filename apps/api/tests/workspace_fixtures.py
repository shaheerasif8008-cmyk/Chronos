"""Database helpers for tests that seed rows below the provisioning API.

Production organization provisioning creates the native default workspace and
memberships before chat is available. Tests that insert organizations or
conversations directly must reproduce that invariant explicitly now that every
conversation has a tenant-bound, non-null workspace.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db import engine, reflect_table


async def ensure_default_workspace(
    organization_id: str,
    member_ids: Iterable[str] = (),
    *,
    region: str = "us",
) -> str:
    workspaces = await reflect_table("workspaces")
    memberships = await reflect_table("workspace_members")
    normalized_members = [str(member_id) for member_id in member_ids]
    created_by = normalized_members[0] if normalized_members else "system"

    async with engine.begin() as conn:
        workspace_id = (
            await conn.execute(
                select(workspaces.c.id).where(
                    workspaces.c.organization_id == organization_id,
                    workspaces.c.legacy_key == "default",
                )
            )
        ).scalar_one_or_none()
        if workspace_id is None:
            workspace_id = (
                await conn.execute(
                    insert(workspaces)
                    .values(
                        organization_id=organization_id,
                        region=region,
                        name="Default workspace",
                        slug="default",
                        legacy_key="default",
                        status="active",
                        created_by=created_by,
                    )
                    .returning(workspaces.c.id)
                )
            ).scalar_one()

        for member_id in normalized_members:
            await conn.execute(
                pg_insert(memberships)
                .values(
                    organization_id=organization_id,
                    region=region,
                    workspace_id=str(workspace_id),
                    member_id=member_id,
                    role="owner" if member_id == created_by else "editor",
                    added_by=created_by,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        memberships.c.organization_id,
                        memberships.c.workspace_id,
                        memberships.c.member_id,
                    ]
                )
            )

    return str(workspace_id)
