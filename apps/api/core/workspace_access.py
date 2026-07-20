"""Canonical native workspace resolution and membership authorization."""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import or_, select

from core.db import engine, reflect_table
from core.exceptions import PermissionDenied
from core.models import Member


AccessLevel = Literal["read", "write", "manage"]
_ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}
_REQUIRED_RANK = {"read": 1, "write": 2, "manage": 3}


async def require_workspace_access(
    actor: Member,
    workspace_id: str | None,
    *,
    access: AccessLevel,
) -> dict[str, Any]:
    """Resolve an id or the legacy `default` alias and enforce live access.

    Archived workspaces remain readable for retention/export purposes but
    reject new writes. Deletion-pending and deleted workspaces reject all use,
    including internal runtime actors, so tombstoning cannot be bypassed by an
    already queued request.
    """

    identifier = str(workspace_id or "default")
    workspaces = await reflect_table("workspaces")
    memberships = await reflect_table("workspace_members")
    async with engine.begin() as conn:
        workspace = (
            await conn.execute(
                select(workspaces).where(
                    workspaces.c.organization_id == actor.organization_id,
                    or_(
                        workspaces.c.id == identifier,
                        workspaces.c.legacy_key == identifier,
                    ),
                )
            )
        ).mappings().first()
        if workspace is None:
            raise PermissionDenied(actor.id, f"{access}_workspace", identifier)
        status = str(workspace["status"])
        if status in {"deletion_pending", "deleted"} or (
            status == "archived" and access != "read"
        ):
            raise PermissionDenied(actor.id, f"{access}_workspace", identifier)
        if actor.role in {"agent", "system"} or actor.id in {"chronos", "scheduler", "system"}:
            return dict(workspace)
        membership = (
            await conn.execute(
                select(memberships.c.role).where(
                    memberships.c.organization_id == actor.organization_id,
                    memberships.c.workspace_id == workspace["id"],
                    memberships.c.member_id == actor.id,
                )
            )
        ).first()
    if membership is None or _ROLE_RANK.get(str(membership[0]), 0) < _REQUIRED_RANK[access]:
        raise PermissionDenied(actor.id, f"{access}_workspace", identifier)
    return dict(workspace)
