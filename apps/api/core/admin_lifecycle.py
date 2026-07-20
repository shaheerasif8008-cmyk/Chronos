"""Tenant-safe native groups, workspaces, and organization lifecycle."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from core import audit, permissions, retention
from core.db import engine, reflect_table
from core.models import Member


log = logging.getLogger(__name__)
WORKSPACE_ROLES = {"owner", "editor", "viewer"}


class LifecycleConflict(Exception):
    pass


class LifecycleNotFound(Exception):
    pass


class LifecycleHeld(Exception):
    pass


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not normalized:
        raise ValueError("Workspace name must include a letter or number")
    return normalized[:80]


async def _active_member(conn, members, org_id: str, member_id: str):
    return (
        await conn.execute(
            select(members).where(
                members.c.id == member_id,
                members.c.organization_id == org_id,
                members.c.status == "active",
            )
        )
    ).mappings().first()


async def list_groups(actor: Member) -> list[dict[str, Any]]:
    groups = await reflect_table("native_groups")
    memberships = await reflect_table("native_group_members")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        group_rows = (
            await conn.execute(
                select(groups)
                .where(groups.c.organization_id == actor.organization_id)
                .order_by(groups.c.name)
            )
        ).mappings().all()
        member_rows = (
            await conn.execute(
                select(
                    memberships.c.group_id,
                    members.c.id,
                    members.c.email,
                    members.c.name,
                    members.c.role,
                    members.c.status,
                )
                .select_from(memberships.join(members, memberships.c.member_id == members.c.id))
                .where(
                    memberships.c.organization_id == actor.organization_id,
                    members.c.organization_id == actor.organization_id,
                )
                .order_by(members.c.email)
            )
        ).mappings().all()
    by_group: dict[str, list[dict[str, Any]]] = {}
    for row in member_rows:
        by_group.setdefault(str(row["group_id"]), []).append(
            {key: row[key] for key in ("id", "email", "name", "role", "status")}
        )
    return [{**dict(row), "members": by_group.get(str(row["id"]), [])} for row in group_rows]


async def create_group(actor: Member, *, name: str, description: str | None) -> dict[str, Any]:
    groups = await reflect_table("native_groups")
    normalized = name.strip()
    if not 1 <= len(normalized) <= 120:
        raise ValueError("Group name must be between 1 and 120 characters")
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(groups)
                    .values(
                        organization_id=actor.organization_id,
                        region=actor.region,
                        name=normalized,
                        description=(description or "").strip() or None,
                        created_by=actor.id,
                    )
                    .returning(groups)
                )
            ).mappings().one()
    except IntegrityError as exc:
        raise LifecycleConflict("A group with this name already exists") from exc
    result = {**dict(row), "members": []}
    await audit.log(
        "organization_change", actor.id, "native_group.created",
        organization_id=actor.organization_id, resource_type="native_group",
        resource_id=str(row["id"]), payload={"name": normalized}, decision="created",
    )
    return result


async def update_group(actor: Member, group_id: str, *, name: str, description: str | None) -> dict[str, Any]:
    groups = await reflect_table("native_groups")
    normalized = name.strip()
    if not 1 <= len(normalized) <= 120:
        raise ValueError("Group name must be between 1 and 120 characters")
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(groups)
                    .where(groups.c.id == group_id, groups.c.organization_id == actor.organization_id)
                    .values(name=normalized, description=(description or "").strip() or None, updated_at=func.now())
                    .returning(groups)
                )
            ).mappings().first()
    except IntegrityError as exc:
        raise LifecycleConflict("A group with this name already exists") from exc
    if row is None:
        raise LifecycleNotFound(group_id)
    await audit.log(
        "organization_change", actor.id, "native_group.updated",
        organization_id=actor.organization_id, resource_type="native_group",
        resource_id=group_id, payload={"name": normalized}, decision="updated",
    )
    return dict(row)


async def delete_group(actor: Member, group_id: str) -> None:
    groups = await reflect_table("native_groups")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                delete(groups)
                .where(groups.c.id == group_id, groups.c.organization_id == actor.organization_id)
                .returning(groups.c.id, groups.c.name)
            )
        ).mappings().first()
    if row is None:
        raise LifecycleNotFound(group_id)
    await audit.log(
        "organization_change", actor.id, "native_group.deleted",
        organization_id=actor.organization_id, resource_type="native_group",
        resource_id=group_id, payload={"name": row["name"]}, decision="deleted",
    )


async def add_group_member(actor: Member, group_id: str, member_id: str) -> None:
    groups = await reflect_table("native_groups")
    memberships = await reflect_table("native_group_members")
    members = await reflect_table("members")
    try:
        async with engine.begin() as conn:
            group = (
                await conn.execute(select(groups.c.id).where(groups.c.id == group_id, groups.c.organization_id == actor.organization_id))
            ).first()
            if group is None:
                raise LifecycleNotFound(group_id)
            if await _active_member(conn, members, actor.organization_id, member_id) is None:
                raise LifecycleNotFound(member_id)
            await conn.execute(
                insert(memberships).values(
                    organization_id=actor.organization_id, region=actor.region,
                    group_id=group_id, member_id=member_id, added_by=actor.id,
                )
            )
    except IntegrityError as exc:
        raise LifecycleConflict("Member already belongs to this group") from exc
    await audit.log(
        "organization_change", actor.id, "native_group.member_added",
        organization_id=actor.organization_id, resource_type="native_group",
        resource_id=group_id, payload={"member_id": member_id}, decision="added",
    )


async def remove_group_member(actor: Member, group_id: str, member_id: str) -> None:
    memberships = await reflect_table("native_group_members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                delete(memberships).where(
                    memberships.c.organization_id == actor.organization_id,
                    memberships.c.group_id == group_id,
                    memberships.c.member_id == member_id,
                ).returning(memberships.c.id)
            )
        ).first()
    if row is None:
        raise LifecycleNotFound(member_id)
    await audit.log(
        "organization_change", actor.id, "native_group.member_removed",
        organization_id=actor.organization_id, resource_type="native_group",
        resource_id=group_id, payload={"member_id": member_id}, decision="removed",
    )


async def list_workspaces(actor: Member) -> list[dict[str, Any]]:
    workspaces = await reflect_table("workspaces")
    workspace_members = await reflect_table("workspace_members")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(workspaces)
                .where(workspaces.c.organization_id == actor.organization_id)
                .order_by(workspaces.c.created_at)
            )
        ).mappings().all()
        member_rows = (
            await conn.execute(
                select(workspace_members.c.workspace_id, workspace_members.c.role, members.c.id, members.c.email, members.c.name, members.c.status)
                .select_from(workspace_members.join(members, workspace_members.c.member_id == members.c.id))
                .where(
                    workspace_members.c.organization_id == actor.organization_id,
                    members.c.organization_id == actor.organization_id,
                )
                .order_by(members.c.email)
            )
        ).mappings().all()
    by_workspace: dict[str, list[dict[str, Any]]] = {}
    for row in member_rows:
        by_workspace.setdefault(str(row["workspace_id"]), []).append(
            {key: row[key] for key in ("id", "email", "name", "role", "status")}
        )
    return [{**dict(row), "members": by_workspace.get(str(row["id"]), [])} for row in rows]


async def list_accessible_workspaces(actor: Member) -> list[dict[str, Any]]:
    """Return only live workspaces in which the current member participates."""

    workspaces = await reflect_table("workspaces")
    workspace_members = await reflect_table("workspace_members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    workspaces.c.id,
                    workspaces.c.name,
                    workspaces.c.slug,
                    workspaces.c.legacy_key,
                    workspaces.c.description,
                    workspaces.c.status,
                    workspace_members.c.role,
                )
                .select_from(
                    workspace_members.join(
                        workspaces, workspace_members.c.workspace_id == workspaces.c.id
                    )
                )
                .where(
                    workspace_members.c.organization_id == actor.organization_id,
                    workspace_members.c.member_id == actor.id,
                    workspaces.c.organization_id == actor.organization_id,
                    workspaces.c.status.in_(["active", "archived"]),
                )
                .order_by(workspaces.c.name)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def create_workspace(actor: Member, *, name: str, description: str | None) -> dict[str, Any]:
    workspaces = await reflect_table("workspaces")
    workspace_members = await reflect_table("workspace_members")
    normalized = name.strip()
    if not 1 <= len(normalized) <= 120:
        raise ValueError("Workspace name must be between 1 and 120 characters")
    slug = _slug(normalized)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(workspaces).values(
                        organization_id=actor.organization_id, region=actor.region,
                        name=normalized, slug=slug, description=(description or "").strip() or None,
                        created_by=actor.id,
                    ).returning(workspaces)
                )
            ).mappings().one()
            await conn.execute(
                insert(workspace_members).values(
                    organization_id=actor.organization_id, region=actor.region,
                    workspace_id=row["id"], member_id=actor.id, role="owner", added_by=actor.id,
                )
            )
    except IntegrityError as exc:
        raise LifecycleConflict("A workspace with this name already exists") from exc
    await permissions.grant_workspace_role(actor.id, "owner", str(row["id"]), actor.organization_id)
    result = {**dict(row), "members": [{"id": actor.id, "email": actor.email, "name": actor.name, "role": "owner", "status": actor.status}]}
    await audit.log(
        "organization_change", actor.id, "workspace.created",
        organization_id=actor.organization_id, resource_type="workspace",
        resource_id=str(row["id"]), payload={"name": normalized, "slug": slug}, decision="created",
    )
    return result


async def update_workspace(actor: Member, workspace_id: str, *, name: str, description: str | None) -> dict[str, Any]:
    workspaces = await reflect_table("workspaces")
    normalized = name.strip()
    if not 1 <= len(normalized) <= 120:
        raise ValueError("Workspace name must be between 1 and 120 characters")
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(workspaces).where(
                        workspaces.c.id == workspace_id,
                        workspaces.c.organization_id == actor.organization_id,
                        workspaces.c.status != "deleted",
                    ).values(name=normalized, slug=_slug(normalized), description=(description or "").strip() or None, updated_at=func.now()).returning(workspaces)
                )
            ).mappings().first()
    except IntegrityError as exc:
        raise LifecycleConflict("A workspace with this name already exists") from exc
    if row is None:
        raise LifecycleNotFound(workspace_id)
    await audit.log(
        "organization_change", actor.id, "workspace.updated",
        organization_id=actor.organization_id, resource_type="workspace",
        resource_id=workspace_id, payload={"name": normalized}, decision="updated",
    )
    return dict(row)


async def set_workspace_member(actor: Member, workspace_id: str, member_id: str, role: str) -> None:
    if role not in WORKSPACE_ROLES:
        raise ValueError("Workspace role must be owner, editor, or viewer")
    workspaces = await reflect_table("workspaces")
    workspace_members = await reflect_table("workspace_members")
    members = await reflect_table("members")
    previous_role: str | None = None
    async with engine.begin() as conn:
        workspace = (
            await conn.execute(select(workspaces.c.id).where(
                workspaces.c.id == workspace_id,
                workspaces.c.organization_id == actor.organization_id,
                workspaces.c.status.in_(["active", "archived"]),
            ).with_for_update())
        ).first()
        if workspace is None or await _active_member(conn, members, actor.organization_id, member_id) is None:
            raise LifecycleNotFound(member_id)
        current = (
            await conn.execute(select(workspace_members).where(
                workspace_members.c.organization_id == actor.organization_id,
                workspace_members.c.workspace_id == workspace_id,
                workspace_members.c.member_id == member_id,
            ).with_for_update())
        ).mappings().first()
        if current is not None:
            previous_role = str(current["role"])
            if previous_role == "owner" and role != "owner":
                owner_count = int((await conn.execute(select(func.count()).select_from(workspace_members).where(
                    workspace_members.c.organization_id == actor.organization_id,
                    workspace_members.c.workspace_id == workspace_id,
                    workspace_members.c.role == "owner",
                ))).scalar_one())
                if owner_count <= 1:
                    raise LifecycleConflict("A workspace must keep at least one owner")
            await conn.execute(update(workspace_members).where(workspace_members.c.id == current["id"]).values(role=role, updated_at=func.now()))
        else:
            await conn.execute(insert(workspace_members).values(
                organization_id=actor.organization_id, region=actor.region,
                workspace_id=workspace_id, member_id=member_id, role=role, added_by=actor.id,
            ))
    if previous_role and previous_role != role:
        await permissions.revoke_workspace_role(member_id, previous_role, workspace_id)
    await permissions.grant_workspace_role(member_id, role, workspace_id, actor.organization_id)
    await audit.log(
        "organization_change", actor.id, "workspace.member_set",
        organization_id=actor.organization_id, resource_type="workspace",
        resource_id=workspace_id, payload={"member_id": member_id, "role": role}, decision="updated",
    )


async def remove_workspace_member(actor: Member, workspace_id: str, member_id: str) -> None:
    workspace_members = await reflect_table("workspace_members")
    async with engine.begin() as conn:
        current = (
            await conn.execute(select(workspace_members).where(
                workspace_members.c.organization_id == actor.organization_id,
                workspace_members.c.workspace_id == workspace_id,
                workspace_members.c.member_id == member_id,
            ).with_for_update())
        ).mappings().first()
        if current is None:
            raise LifecycleNotFound(member_id)
        if current["role"] == "owner":
            owner_count = int((await conn.execute(select(func.count()).select_from(workspace_members).where(
                workspace_members.c.organization_id == actor.organization_id,
                workspace_members.c.workspace_id == workspace_id,
                workspace_members.c.role == "owner",
            ))).scalar_one())
            if owner_count <= 1:
                raise LifecycleConflict("A workspace must keep at least one owner")
        await conn.execute(delete(workspace_members).where(workspace_members.c.id == current["id"]))
    await permissions.revoke_workspace_role(member_id, str(current["role"]), workspace_id)
    await audit.log(
        "organization_change", actor.id, "workspace.member_removed",
        organization_id=actor.organization_id, resource_type="workspace",
        resource_id=workspace_id, payload={"member_id": member_id}, decision="removed",
    )


async def archive_workspace(actor: Member, workspace_id: str, *, archived: bool) -> dict[str, Any]:
    workspaces = await reflect_table("workspaces")
    desired = "archived" if archived else "active"
    async with engine.begin() as conn:
        row = (
            await conn.execute(update(workspaces).where(
                workspaces.c.id == workspace_id,
                workspaces.c.organization_id == actor.organization_id,
                workspaces.c.status.in_(["active", "archived"]),
            ).values(status=desired, archived_at=func.now() if archived else None, updated_at=func.now()).returning(workspaces))
        ).mappings().first()
    if row is None:
        raise LifecycleNotFound(workspace_id)
    await audit.log(
        "organization_change", actor.id, f"workspace.{desired}",
        organization_id=actor.organization_id, resource_type="workspace",
        resource_id=workspace_id, decision=desired,
    )
    return dict(row)


async def _workspace_held(org_id: str, workspace_id: str) -> bool:
    rows = await retention.list_holds(org_id, active_only=True)
    return any(
        (row["resource_type"] == "organization" and str(row["resource_id"]) == str(org_id))
        or (row["resource_type"] == "workspace" and str(row["resource_id"]) == str(workspace_id))
        for row in rows
    )


async def request_workspace_deletion(actor: Member, workspace_id: str, confirmation: str) -> dict[str, Any]:
    workspaces = await reflect_table("workspaces")
    async with retention._org_retention_lock(actor.organization_id):
        if await _workspace_held(actor.organization_id, workspace_id):
            raise LifecycleHeld(workspace_id)
        policy = await retention.load_policy(actor.organization_id)
        if not policy.configuration_valid:
            raise LifecycleConflict("Retention configuration is invalid; deletion is blocked")
        async with engine.begin() as conn:
            current = (
                await conn.execute(select(workspaces).where(
                    workspaces.c.id == workspace_id,
                    workspaces.c.organization_id == actor.organization_id,
                    workspaces.c.status.in_(["active", "archived"]),
                ).with_for_update())
            ).mappings().first()
            if current is None:
                raise LifecycleNotFound(workspace_id)
            if confirmation != f"DELETE {current['name']}":
                raise LifecycleConflict(f"Type DELETE {current['name']} to confirm")
            execute_after = datetime.now(timezone.utc) + timedelta(days=max(1, policy.deleted_artifact_days))
            row = (
                await conn.execute(update(workspaces).where(workspaces.c.id == workspace_id).values(
                    status="deletion_pending", archived_at=current.get("archived_at") or func.now(),
                    deletion_requested_at=func.now(), deletion_requested_by=actor.id,
                    deletion_execute_after=execute_after, updated_at=func.now(),
                ).returning(workspaces))
            ).mappings().one()
    await audit.log(
        "organization_change", actor.id, "workspace.deletion_requested",
        organization_id=actor.organization_id, resource_type="workspace",
        resource_id=workspace_id, payload={"execute_after": execute_after.isoformat()}, decision="scheduled",
    )
    return dict(row)


async def cancel_workspace_deletion(actor: Member, workspace_id: str) -> dict[str, Any]:
    workspaces = await reflect_table("workspaces")
    async with engine.begin() as conn:
        row = (
            await conn.execute(update(workspaces).where(
                workspaces.c.id == workspace_id,
                workspaces.c.organization_id == actor.organization_id,
                workspaces.c.status == "deletion_pending",
            ).values(
                status="archived", deletion_requested_at=None, deletion_requested_by=None,
                deletion_execute_after=None, updated_at=func.now(),
            ).returning(workspaces))
        ).mappings().first()
    if row is None:
        raise LifecycleNotFound(workspace_id)
    await audit.log(
        "organization_change", actor.id, "workspace.deletion_cancelled",
        organization_id=actor.organization_id, resource_type="workspace",
        resource_id=workspace_id, decision="cancelled",
    )
    return dict(row)


async def process_due_workspace_deletions() -> dict[str, int]:
    """Leader-scheduled tombstoning. Data remains retained for legal evidence."""
    workspaces = await reflect_table("workspaces")
    workspace_members = await reflect_table("workspace_members")
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        due = (
            await conn.execute(select(workspaces.c.id, workspaces.c.organization_id).where(
                workspaces.c.status == "deletion_pending",
                workspaces.c.deletion_execute_after <= now,
            ))
        ).all()
    deleted_count = 0
    held_count = 0
    for workspace_id, org_id in due:
        async with retention._org_retention_lock(str(org_id)):
            if await _workspace_held(str(org_id), str(workspace_id)):
                held_count += 1
                continue
            async with engine.begin() as conn:
                membership_rows = (
                    await conn.execute(select(workspace_members.c.member_id, workspace_members.c.role).where(
                        workspace_members.c.organization_id == org_id,
                        workspace_members.c.workspace_id == workspace_id,
                    ))
                ).all()
                result = await conn.execute(update(workspaces).where(
                    workspaces.c.id == workspace_id,
                    workspaces.c.organization_id == org_id,
                    workspaces.c.status == "deletion_pending",
                    workspaces.c.deletion_execute_after <= now,
                ).values(status="deleted", deleted_at=func.now(), updated_at=func.now()))
                if int(result.rowcount or 0) == 0:
                    continue
                await conn.execute(delete(workspace_members).where(
                    workspace_members.c.organization_id == org_id,
                    workspace_members.c.workspace_id == workspace_id,
                ))
            for member_id, role in membership_rows:
                try:
                    await permissions.revoke_workspace_role(str(member_id), str(role), str(workspace_id))
                except Exception:
                    log.warning("Workspace tuple revocation will need reconciliation", exc_info=True)
            deleted_count += 1
            await audit.log(
                "organization_change", "scheduler", "workspace.deleted",
                organization_id=str(org_id), resource_type="workspace",
                resource_id=str(workspace_id), payload={"mode": "retained_tombstone"}, decision="deleted",
            )
    return {"deleted": deleted_count, "held": held_count}


async def transfer_ownership(actor: Member, target_member_id: str, confirmation: str) -> None:
    if actor.role != "owner":
        raise PermissionError("Only an organization owner can transfer ownership")
    if target_member_id == actor.id:
        raise LifecycleConflict("Choose another active member")
    members = await reflect_table("members")
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        org = (
            await conn.execute(select(organizations.c.name).where(organizations.c.id == actor.organization_id))
        ).first()
        org_name = str(org[0]) if org else actor.organization_id
        if confirmation != f"TRANSFER {org_name}":
            raise LifecycleConflict(f"Type TRANSFER {org_name} to confirm")
        rows = (
            await conn.execute(select(members).where(
                members.c.organization_id == actor.organization_id,
                members.c.id.in_([actor.id, target_member_id]),
            ).with_for_update())
        ).mappings().all()
        by_id = {str(row["id"]): row for row in rows}
        target = by_id.get(target_member_id)
        if target is None or target.get("status", "active") != "active":
            raise LifecycleNotFound(target_member_id)
        await conn.execute(update(members).where(members.c.id == target_member_id, members.c.organization_id == actor.organization_id).values(role="owner"))
        await conn.execute(update(members).where(members.c.id == actor.id, members.c.organization_id == actor.organization_id).values(role="admin"))
    await permissions.sync_org_membership(target_member_id, actor.organization_id, role="owner", active=True)
    await permissions.sync_org_membership(actor.id, actor.organization_id, role="admin", active=True)
    await audit.log(
        "organization_change", actor.id, "organization.ownership_transferred",
        organization_id=actor.organization_id, resource_type="organization",
        resource_id=actor.organization_id, payload={"target_member_id": target_member_id}, decision="transferred",
    )


async def leave_organization(actor: Member, confirmation: str) -> None:
    members = await reflect_table("members")
    organizations = await reflect_table("organizations")
    keys = await reflect_table("organization_api_keys")
    async with engine.begin() as conn:
        org = (
            await conn.execute(select(organizations.c.name).where(organizations.c.id == actor.organization_id))
        ).first()
        org_name = str(org[0]) if org else actor.organization_id
        if confirmation != f"LEAVE {org_name}":
            raise LifecycleConflict(f"Type LEAVE {org_name} to confirm")
        current = (
            await conn.execute(select(members).where(
                members.c.id == actor.id,
                members.c.organization_id == actor.organization_id,
                members.c.status == "active",
            ).with_for_update())
        ).mappings().first()
        if current is None:
            raise LifecycleNotFound(actor.id)
        if current["role"] == "owner":
            owner_count = int((await conn.execute(select(func.count()).select_from(members).where(
                members.c.organization_id == actor.organization_id,
                members.c.status == "active",
                members.c.role == "owner",
            ))).scalar_one())
            if owner_count <= 1:
                raise LifecycleConflict("Transfer ownership before the last owner leaves")
        await conn.execute(update(members).where(members.c.id == actor.id).values(status="deactivated"))
        await conn.execute(update(keys).where(
            keys.c.organization_id == actor.organization_id,
            keys.c.created_by_member_id == actor.id,
            keys.c.status == "active",
        ).values(status="revoked", revoked_at=func.now(), revoked_by=actor.id, updated_at=func.now()))
    await permissions.sync_org_membership(actor.id, actor.organization_id, role=actor.role, active=False)
    await audit.log(
        "organization_change", actor.id, "organization.member_left",
        organization_id=actor.organization_id, resource_type="organization",
        resource_id=actor.organization_id, decision="deactivated",
    )
