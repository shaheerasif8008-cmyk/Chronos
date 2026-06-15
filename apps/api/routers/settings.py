from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update

from core import audit, invitations, permissions
from core.auth import get_current_member
from core.connector_health import check_connectors
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from core.memory_control import export_memories
from core.settings_store import (
    ADMIN_ROLES,
    AUTONOMY_LEVELS,
    DEFAULTS,
    ROLE_ORDER,
    get_settings_doc,
    require_admin,
    save_settings_doc,
    workspace_autonomy,
)

router = APIRouter(prefix="/settings", tags=["settings"])

ADMIN_SECTIONS = {
    "organization",
    "members",
    "permissions",
    "ai_employee",
    "runtime",
    "memory",
    "tool_settings",
    "approval",
    "developer",
    "danger",
}


class SettingsPatch(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class MemberRolePatch(BaseModel):
    role: str


class AutonomyPatch(BaseModel):
    workspace_id: str = "default"
    level: str


class InvitationCreate(BaseModel):
    email: str
    role: str = "viewer"


class MemoryPurgeRequest(BaseModel):
    confirmation: str


def _unsupported(reason: str) -> dict[str, Any]:
    return {"supported": False, "reason": reason}


def _validate_section(section: str, values: dict[str, Any]) -> None:
    if section == "general":
        if not str(values.get("workspace_name", "x")).strip():
            raise HTTPException(status_code=400, detail="Workspace name is required")
        if values.get("default_landing_page") and values["default_landing_page"] not in {"chat", "activity", "approvals", "memory", "connectors", "assistants"}:
            raise HTTPException(status_code=400, detail="Invalid default landing page")
        if values.get("theme") and values["theme"] not in {"light", "dark", "system"}:
            raise HTTPException(status_code=400, detail="Invalid theme")
    if section == "profile":
        if values.get("preferred_response_length") and values["preferred_response_length"] not in {"short", "medium", "long"}:
            raise HTTPException(status_code=400, detail="Invalid response length")
    if section == "runtime":
        if int(values.get("max_task_queue_size", 1)) < 1:
            raise HTTPException(status_code=400, detail="Task queue size must be positive")
    if section == "ai_employee":
        if int(values.get("max_sub_agent_depth", 1)) > 3 and not values.get("sub_agent_spawning"):
            raise HTTPException(status_code=400, detail="Sub-agent depth above 3 requires spawning to be enabled")


async def _current_org(member: Member) -> dict[str, Any]:
    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        org = (
            await conn.execute(select(organizations).where(organizations.c.id == member.organization_id))
        ).mappings().first()
        member_count = (
            await conn.execute(select(func.count()).select_from(members).where(members.c.organization_id == member.organization_id))
        ).scalar_one()
    data = dict(org or {})
    org_settings = await get_settings_doc(member, "organization")
    return {
        "id": data.get("id", member.organization_id),
        "name": org_settings.get("organization_name") or data.get("name", member.organization_id),
        "slug": data.get("slug", "default"),
        "domain": org_settings.get("domain", ""),
        "logo": org_settings.get("logo", ""),
        "plan": org_settings.get("plan") or data.get("plan", "trial"),
        "seats": member_count,
        "owner": "Owner/Admin",
        "can_edit": member.role in ADMIN_ROLES,
        "default_workspace_creation": org_settings.get("default_workspace_creation", "admins"),
    }


async def _members(member: Member) -> list[dict[str, Any]]:
    members = await reflect_table("members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(members).where(members.c.organization_id == member.organization_id).order_by(members.c.created_at.asc())
            )
        ).mappings().all()
    return [
        {
            "id": row["id"],
            "name": row.get("name") or row["email"],
            "email": row["email"],
            "role": row["role"],
            "status": "active",
            "created_at": str(row["created_at"]) if row.get("created_at") else None,
            "is_self": row["id"] == member.id,
        }
        for row in rows
    ]


async def _connectors(member: Member) -> list[dict[str, Any]]:
    connectors = await reflect_table("connectors")
    tool_settings = await get_settings_doc(member, "tool_settings")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(select(connectors).where(connectors.c.organization_id == member.organization_id))
        ).mappings().all()
    return [
        {
            "id": row["id"],
            "provider": row["provider"],
            "account_handle": row.get("account_handle"),
            "status": row["status"],
            "scopes": row.get("scopes") or [],
            "last_used_at": str(row["last_used_at"]) if row.get("last_used_at") else None,
            "connected_at": str(row["connected_at"]) if row.get("connected_at") else None,
            "policy": tool_settings.get(row["provider"], {}),
        }
        for row in rows
    ]


async def _memory_stats(member: Member) -> dict[str, Any]:
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        active = (
            await conn.execute(
                select(func.count()).select_from(memory_entries).where(
                    memory_entries.c.organization_id == member.organization_id,
                    memory_entries.c.is_deleted.is_(False),
                )
            )
        ).scalar_one()
        deleted = (
            await conn.execute(
                select(func.count()).select_from(memory_entries).where(
                    memory_entries.c.organization_id == member.organization_id,
                    memory_entries.c.is_deleted.is_(True),
                )
            )
        ).scalar_one()
    return {"active": active, "deleted": deleted}


@router.get("/")
async def overview(member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "read_settings", member.organization_id)
    sections = {
        name: await get_settings_doc(member, name)
        for name in [
            "general",
            "profile",
            "permissions",
            "ai_employee",
            "runtime",
            "memory",
            "tool_settings",
            "approval",
            "notifications",
            "developer",
            "response_format",
        ]
    }
    sections["profile"]["email"] = member.email
    sections["profile"]["role"] = member.role
    sections["profile"]["display_name"] = sections["profile"].get("display_name") or member.name or ""
    org = await _current_org(member)
    sections["organization"] = {**DEFAULTS["organization"], **org}
    return {
        "member": {"id": member.id, "email": member.email, "name": member.name, "role": member.role, "can_admin": member.role in ADMIN_ROLES},
        "organization": org,
        "sections": sections,
        "members": await _members(member),
        "connectors": await _connectors(member),
        "memory_stats": await _memory_stats(member),
        "runtime_health": {
            "status": "ok",
            "mode": sections["runtime"]["runtime_mode"],
            "incomplete_task_recovery": "enabled",
            "connectors": await check_connectors(),
        },
        "capabilities": {
            "email_edit": _unsupported("OTP auth does not support email changes."),
            "profile_photo_upload": _unsupported("No file upload service is configured."),
            "invitations": {"supported": True, "delivery": "manual_token"},
            "sessions": _unsupported("JWT sessions are stateless and not persisted."),
            "password": _unsupported("OTP auth has no password credential."),
            "two_factor": _unsupported("OTP login is the configured second factor."),
            "api_keys": _unsupported("API key authentication is not implemented."),
            "billing": _unsupported("No billing provider is configured."),
            "webhooks": _unsupported("Webhook dispatcher is not implemented."),
            "notification_email_dispatch": _unsupported("Email notification delivery service is not configured."),
            "delete_workspace": _unsupported("Workspace deletion has no archival workflow yet."),
            "transfer_ownership": _unsupported("Ownership transfer is not implemented."),
        },
    }


@router.patch("/{section}")
async def update_section(section: str, req: SettingsPatch, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    if section not in DEFAULTS:
        raise HTTPException(status_code=404, detail="Unknown settings section")
    if section in ADMIN_SECTIONS:
        require_admin(member)
    _validate_section(section, req.values)
    saved = await save_settings_doc(member, section, req.values)
    return {"section": section, "values": saved}


@router.get("/autonomy")
async def get_workspace_autonomy(
    workspace_id: str = Query("default"),
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    """Return the autonomy level for a workspace (default 'supervised')."""
    await permissions.check(member, "get_autonomy", settings.org_id)
    level = await workspace_autonomy(member.organization_id, workspace_id)
    return {"workspace_id": workspace_id, "level": level, "levels": sorted(AUTONOMY_LEVELS)}


@router.patch("/autonomy")
async def set_workspace_autonomy(
    req: AutonomyPatch, member: Member = Depends(get_current_member)
) -> dict[str, Any]:
    """Set a workspace's autonomy level. Admin-only.

    ``full_auto`` collapses settings-policy approval gates for that workspace.
    The hard floor (external publish, payments, gmail.send, local shell) and all
    safety limits in the tool broker remain absolute regardless of this setting.
    """
    require_admin(member)
    if req.level not in AUTONOMY_LEVELS:
        raise HTTPException(status_code=400, detail="Invalid autonomy level")
    await save_settings_doc(
        member,
        "autonomy",
        {"level": req.level},
        scope="workspace",
        scope_id=req.workspace_id,
    )
    return {"workspace_id": req.workspace_id, "level": req.level}


@router.patch("/members/{member_id}/role")
async def update_member_role(member_id: str, req: MemberRolePatch, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    require_admin(member)
    if req.role not in ROLE_ORDER:
        raise HTTPException(status_code=400, detail="Invalid role")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        target = (
            await conn.execute(
                select(members).where(members.c.id == member_id, members.c.organization_id == member.organization_id)
            )
        ).mappings().first()
        if not target:
            raise HTTPException(status_code=404, detail="Member not found")
        owner_count = (
            await conn.execute(
                select(func.count()).select_from(members).where(
                    members.c.organization_id == member.organization_id,
                    members.c.role.in_(["owner", "admin"]),
                )
            )
        ).scalar_one()
        if target["role"] in ADMIN_ROLES and req.role not in ADMIN_ROLES and owner_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote the last owner/admin")
        await conn.execute(update(members).where(members.c.id == member_id).values(role=req.role))
    await audit.log("settings_change", member.id, "settings.members.role_update", organization_id=member.organization_id, resource_type="members", resource_id=member_id, payload={"role": req.role})
    return {"id": member_id, "role": req.role}


@router.get("/invitations")
async def list_member_invitations(member: Member = Depends(get_current_member)) -> dict[str, Any]:
    require_admin(member)
    return {"invitations": await invitations.list_invitations(member.organization_id)}


@router.post("/invitations")
async def create_member_invitation(req: InvitationCreate, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    require_admin(member)
    email = req.email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=400, detail="A valid email is required")
    if req.role not in ROLE_ORDER:
        raise HTTPException(status_code=400, detail="Invalid role")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                select(members.c.id).where(
                    members.c.organization_id == member.organization_id,
                    members.c.email == email,
                )
            )
        ).first()
    if existing:
        raise HTTPException(status_code=409, detail="That email is already a member")
    return await invitations.create_invitation(member, email, req.role)


@router.post("/invitations/{invitation_id}/revoke")
async def revoke_member_invitation(invitation_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    require_admin(member)
    if not await invitations.revoke_invitation(member, invitation_id):
        raise HTTPException(status_code=404, detail="No pending invitation with that id")
    return {"id": invitation_id, "status": "revoked"}


@router.delete("/members/{member_id}")
async def deactivate_member(member_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    require_admin(member)
    if member_id == member.id:
        raise HTTPException(status_code=400, detail="Cannot remove your own account")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        target = (
            await conn.execute(
                select(members).where(members.c.id == member_id, members.c.organization_id == member.organization_id)
            )
        ).mappings().first()
        if not target:
            raise HTTPException(status_code=404, detail="Member not found")
        owner_count = (
            await conn.execute(
                select(func.count()).select_from(members).where(
                    members.c.organization_id == member.organization_id,
                    members.c.role.in_(["owner", "admin"]),
                )
            )
        ).scalar_one()
        if target["role"] in ADMIN_ROLES and owner_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot remove the last owner/admin")
        await conn.execute(delete(members).where(members.c.id == member_id))
    await audit.log("settings_change", member.id, "settings.members.remove", organization_id=member.organization_id, resource_type="members", resource_id=member_id)
    return {"id": member_id, "removed": True}


@router.post("/memory/purge")
async def purge_memory(req: MemoryPurgeRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    require_admin(member)
    if req.confirmation != "PURGE MEMORY":
        raise HTTPException(status_code=400, detail='Type "PURGE MEMORY" to confirm')
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        result = await conn.execute(
            update(memory_entries)
            .where(memory_entries.c.organization_id == member.organization_id, memory_entries.c.is_deleted.is_(False))
            .values(is_deleted=True, updated_at=func.now())
        )
    await audit.log("settings_change", member.id, "settings.memory.purge", organization_id=member.organization_id, resource_type="memory", payload={"deleted": result.rowcount}, decision="confirmed")
    return {"deleted": result.rowcount}


@router.get("/memory/export.json")
async def export_memory(member: Member = Depends(get_current_member)) -> JSONResponse:
    require_admin(member)
    items = await export_memories(member)
    return JSONResponse(
        {
            "format": "json",
            "scope": "organization",
            "scope_id": member.organization_id,
            "include": "non-deleted memories, including archived and excluding superseded",
            "items": items,
        },
        headers={"Content-Disposition": "attachment; filename=chronos-memory-org-export.json"},
    )


@router.get("/audit")
async def list_audit(
    actor: str | None = None,
    action: str | None = None,
    query: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_audit_log", member.organization_id)
    audit_log = await reflect_table("audit_log")
    stmt = select(audit_log).where(audit_log.c.organization_id == member.organization_id)
    if actor:
        stmt = stmt.where(audit_log.c.actor_id == actor)
    if action:
        stmt = stmt.where(audit_log.c.action == action)
    if query:
        stmt = stmt.where(audit_log.c.action.ilike(f"%{query}%"))
    stmt = stmt.order_by(audit_log.c.created_at.desc()).limit(limit).offset(offset)
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(row) for row in rows]


@router.get("/audit/export.csv")
async def export_audit(member: Member = Depends(get_current_member)) -> StreamingResponse:
    rows = await list_audit(limit=500, member=member)
    handle = io.StringIO()
    writer = csv.DictWriter(handle, fieldnames=["id", "created_at", "actor_id", "event_type", "action", "resource_type", "resource_id", "decision"])
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key in writer.fieldnames})
    handle.seek(0)
    return StreamingResponse(iter([handle.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=chronos-audit.csv"})
