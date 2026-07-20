from __future__ import annotations

import csv
import dataclasses
import io
import json
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from core import (
    audit,
    billing,
    invitations,
    notification_delivery,
    permissions,
    retention,
    runtime_health,
)
from core.plans import get_entitlements
from core.auth import get_current_member
from core.connector_health import check_connectors
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from core.memory_control import export_memories
from core.settings_store import (
    ADMIN_ROLES,
    AI_EMPLOYEE_SETTING_KEYS,
    AUTONOMY_LEVELS,
    DEFAULTS,
    MEMORY_SETTING_KEYS,
    ROLE_ORDER,
    RUNTIME_SETTING_KEYS,
    get_settings_doc,
    require_admin,
    save_settings_doc,
    workspace_autonomy,
)
from core.governance import usage_summary
from core.token_budget import token_usage_summary

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


class RetentionRunRequest(BaseModel):
    dry_run: bool = True
    confirmation: str | None = None


class RetentionHoldCreate(BaseModel):
    resource_type: Literal["organization", "memory", "artifact", "workspace"]
    resource_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=5, max_length=2000)


async def _safe_token_usage_summary(org_id: str) -> dict[str, Any]:
    try:
        return await token_usage_summary(org_id)
    except Exception:
        return {"metered": False, "tokens_today": 0, "daily_limit": settings.per_org_daily_token_limit, "enforced": False}


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
        if values.get("accent") and values["accent"] not in {"coral", "forest", "indigo", "slate"}:
            raise HTTPException(status_code=400, detail="Invalid accent color")
    if section == "profile":
        if values.get("preferred_response_length") and values["preferred_response_length"] not in {"short", "medium", "long"}:
            raise HTTPException(status_code=400, detail="Invalid response length")
    if section == "notifications":
        allowed = {
            "email",
            "slack",
            "teams",
            "in_app",
            "desktop",
            "runtime_failure_alerts",
            "approval_request_alerts",
            "task_completion_alerts",
            "weekly_digest",
            "security_alerts",
        }
        unsupported = set(values) - allowed
        if unsupported:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported notification settings: {', '.join(sorted(unsupported))}",
            )
        if any(not isinstance(value, bool) for value in values.values()):
            raise HTTPException(
                status_code=400, detail="Notification settings must be boolean"
            )
    if section == "runtime":
        unsupported = set(values) - RUNTIME_SETTING_KEYS
        if unsupported:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported runtime settings: {', '.join(sorted(unsupported))}",
            )
        try:
            queue_size = int(values.get("max_task_queue_size", 1))
            token_budget = int(values.get("token_budget_daily", 0))
            cost_budget = float(values.get("cost_budget_daily_usd", 0))
            request_rate = int(values.get("request_rate_per_minute", 0))
            connector_rate = int(values.get("connector_rate_per_minute", 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Runtime limits must be numeric")
        if queue_size < 1:
            raise HTTPException(status_code=400, detail="Task queue size must be positive")
        if min(token_budget, cost_budget, request_rate, connector_rate) < 0:
            raise HTTPException(status_code=400, detail="Runtime limits cannot be negative")
    if section == "memory":
        unsupported = set(values) - MEMORY_SETTING_KEYS
        if unsupported:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported memory settings: {', '.join(sorted(unsupported))}",
            )
        if "retention_enabled" in values and not isinstance(
            values["retention_enabled"], bool
        ):
            raise HTTPException(
                status_code=400, detail="retention_enabled must be a boolean"
            )
        for key in (
            "retention_days",
            "deleted_retention_days",
            "deleted_artifact_retention_days",
        ):
            if key not in values:
                continue
            try:
                days = int(values[key])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{key} must be a number")
            if not 1 <= days <= 3650:
                raise HTTPException(
                    status_code=400, detail=f"{key} must be between 1 and 3650 days"
                )
    if section == "ai_employee":
        unsupported = set(values) - AI_EMPLOYEE_SETTING_KEYS
        if unsupported:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported AI employee settings: {', '.join(sorted(unsupported))}",
            )
        try:
            max_runtimes = int(values.get("max_concurrent_runtimes", 1))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="Max concurrent runtimes must be numeric"
            )
        if max_runtimes < 1:
            raise HTTPException(
                status_code=400, detail="Max concurrent runtimes must be positive"
            )


async def _current_org(member: Member) -> dict[str, Any]:
    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        org = (
            await conn.execute(select(organizations).where(organizations.c.id == member.organization_id))
        ).mappings().first()
        member_count = (
            await conn.execute(
                select(func.count()).select_from(members).where(
                    members.c.organization_id == member.organization_id,
                    members.c.status == "active",
                )
            )
        ).scalar_one()
        owner = (
            await conn.execute(
                select(members.c.name, members.c.email)
                .where(
                    members.c.organization_id == member.organization_id,
                    members.c.role == "owner",
                    members.c.status == "active",
                )
                .order_by(members.c.created_at.asc())
                .limit(1)
            )
        ).mappings().first()
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
        "owner": (owner.get("name") or owner.get("email")) if owner else None,
        "can_edit": member.role in ADMIN_ROLES,
        "default_workspace_creation": org_settings.get("default_workspace_creation", "admins"),
    }


async def _members(member: Member) -> list[dict[str, Any]]:
    members = await reflect_table("members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(members).where(
                    members.c.organization_id == member.organization_id,
                    *([] if member.role in ADMIN_ROLES else [members.c.id == member.id]),
                ).order_by(members.c.created_at.asc())
            )
        ).mappings().all()
    return [
        {
            "id": row["id"],
            "name": row.get("name") or row["email"],
            "email": row["email"],
            "role": row["role"],
            "status": row.get("status", "active"),
            "created_at": str(row["created_at"]) if row.get("created_at") else None,
            "is_self": row["id"] == member.id,
        }
        for row in rows
    ]


async def _connectors(member: Member) -> list[dict[str, Any]]:
    from core.connector_tools import member_connector_clause

    connectors = await reflect_table("connectors")
    tool_settings = await get_settings_doc(member, "tool_settings")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(connectors).where(
                    connectors.c.organization_id == member.organization_id,
                    member_connector_clause(
                        connectors, str(member.organization_id), str(member.id)
                    ),
                )
            )
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
    is_admin = member.role in ADMIN_ROLES
    section_names = [
        "general",
        "profile",
        "notifications",
        "response_format",
    ]
    if is_admin:
        section_names.extend(
            [
                "permissions",
                "ai_employee",
                "runtime",
                "memory",
                "tool_settings",
                "approval",
                "developer",
            ]
        )
    sections = {
        name: await get_settings_doc(member, name)
        for name in section_names
    }
    # UI preferences are user-scoped even though workspace defaults live in
    # the organization-scoped general document.
    sections["general"].update(sections["profile"].get("ui_preferences") or {})
    sections["profile"]["email"] = member.email
    sections["profile"]["role"] = member.role
    sections["profile"]["display_name"] = sections["profile"].get("display_name") or member.name or ""
    org = await _current_org(member)
    if is_admin:
        sections["organization"] = {**DEFAULTS["organization"], **org}
    else:
        # A normal member needs workspace identity and plan context, but must
        # not receive another member's identity, seat counts, verified domain,
        # logo configuration, or administrative creation policy.
        org = {
            key: org[key]
            for key in ("id", "name", "slug", "plan", "can_edit")
            if key in org
        }
    try:
        usage = await usage_summary(member.organization_id) if is_admin else {
            "tokens": {"metered": False},
            "cost": {"metered": False},
            "suspended": False,
        }
    except Exception:
        usage = {
            "tokens": await _safe_token_usage_summary(member.organization_id),
            "cost": {"metered": False, "cost_today_usd": 0.0, "daily_limit_usd": 0.0, "enforced": False},
            "suspended": False,
        }
    else:
        # Preserve the old token-summary seam so existing tests and callers that
        # patch it still control the token portion of the overview.
        if is_admin:
            usage["tokens"] = await _safe_token_usage_summary(member.organization_id)
    return {
        "member": {"id": member.id, "email": member.email, "name": member.name, "role": member.role, "can_admin": member.role in ADMIN_ROLES},
        "organization": org,
        "sections": sections,
        "members": await _members(member),
        "connectors": await _connectors(member),
        "memory_stats": await _memory_stats(member) if is_admin else {"active": 0, "deleted": 0},
        "usage": usage,
        "runtime_health": {
            # Reaching this authenticated overview proves the API is online; it
            # does not prove every worker/provider is healthy. Connector checks
            # below carry their own verified/configured/error states.
            "status": "api_online",
            "environment": settings.environment,
            "execution_mode": "platform_managed",
            "isolation": (
                "container" if settings.is_production else "local_process"
            ),
            "task_lease_heartbeat_seconds": settings.task_lease_heartbeat_seconds,
            "task_lease_ttl_seconds": settings.task_lease_ttl_seconds,
            "recovery_policy": "automatic_resume",
            "log_retention": "deployment_managed",
            "incomplete_task_recovery": "enabled",
            "connectors": await check_connectors() if is_admin else {},
        },
        "capabilities": {
            "email_edit": _unsupported("OTP auth does not support email changes."),
            "profile_photo_upload": _unsupported("No file upload service is configured."),
            "invitations": ({
                "supported": True,
                "delivery": (
                    "email" if notification_delivery.email_is_configured() else "manual_link"
                ),
            } if is_admin else _unsupported("Admin role required.")),
            "sessions": _unsupported("JWT sessions are stateless and not persisted."),
            "password": _unsupported("OTP auth has no password credential."),
            "two_factor": _unsupported("OTP login is the configured second factor."),
            "api_keys": ({"supported": True, "delivery": "one_time_plaintext"} if is_admin else _unsupported("Admin role required.")),
            "billing": ({"supported": True} if billing.is_configured() else _unsupported("No billing provider is configured.")),
            "webhooks": ({
                "supported": True,
                "delivery": "timestamped_hmac",
                "max_payload_bytes": 1_048_576,
                "public_base_url": settings.oauth_callback_base_url.rstrip("/"),
            } if is_admin else _unsupported("Admin role required.")),
            "notification_email_dispatch": ({"supported": True} if notification_delivery.email_is_configured() else _unsupported("Email notification delivery service is not configured.")),
            "delete_workspace": ({"supported": True, "delivery": "retention_delayed_tombstone"} if is_admin else _unsupported("Admin role required.")),
            "transfer_ownership": ({"supported": True} if member.role == "owner" else _unsupported("Organization owner role required.")),
        },
    }


@router.get("/member-directory")
async def member_directory(
    member: Member = Depends(get_current_member),
) -> dict[str, list[dict[str, str | None]]]:
    """Return the minimal teammate directory needed by collaboration controls.

    Sharing and task handoff APIs accept immutable member ids. Ordinary members
    therefore need a safe way to resolve a teammate's visible identity without
    receiving administrative account metadata, invitation state, auth ids, or
    inactive/foreign-tenant records.
    """

    await permissions.check(
        member, "list_collaboration_members", member.organization_id
    )
    members = await reflect_table("members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    members.c.id,
                    members.c.name,
                    members.c.email,
                    members.c.role,
                )
                .where(
                    members.c.organization_id == member.organization_id,
                    members.c.status == "active",
                )
                .order_by(
                    func.lower(func.coalesce(members.c.name, members.c.email)),
                    func.lower(members.c.email),
                    members.c.id,
                )
            )
        ).mappings().all()
    return {
        "members": [
            {
                "id": str(row["id"]),
                "name": row.get("name") or row["email"],
                "email": str(row["email"]),
                "role": str(row["role"]),
            }
            for row in rows
        ]
    }


@router.get("/onboarding")
async def get_onboarding(member: Member = Depends(get_current_member)) -> dict:
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        state = (await conn.execute(
            select(organizations.c.onboarding_state).where(organizations.c.id == member.organization_id)
        )).scalar_one_or_none()
    return {"state": state or "new"}


@router.get("/onboarding/guide")
async def get_onboarding_guide(
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    """Return server-derived progress for the first successful client workflow.

    The guide is advisory and never weakens runtime readiness. Completion is
    derived from durable tenant records, so refreshes and different browsers
    show the same truthful state instead of a local-only tour.
    """
    await permissions.check(member, "read_settings", member.organization_id)
    connectors = await reflect_table("connectors")
    projects = await reflect_table("projects")
    project_sources = await reflect_table("project_sources")
    research_runs = await reflect_table("research_runs")
    approvals = await reflect_table("approvals")
    scheduled_tasks = await reflect_table("scheduled_tasks")

    async with engine.begin() as conn:
        async def count(table, *conditions) -> int:
            return int(
                (
                    await conn.execute(
                        select(func.count()).select_from(table).where(
                            table.c.organization_id == member.organization_id,
                            *conditions,
                        )
                    )
                ).scalar_one()
            )

        counts = {
            "connector": await count(connectors, connectors.c.status == "active"),
            "project": await count(projects),
            "source": await count(project_sources),
            "research": await count(research_runs),
            "approval": await count(approvals, approvals.c.decided_at.is_not(None)),
            "schedule": await count(scheduled_tasks),
        }

    definitions = [
        ("connector", "Connect a work account", "Authorize one provider with the least scopes needed.", "/connectors?onboarding=connector"),
        ("project", "Create a project", "Set the goal, visibility, instructions, and tool defaults.", "/projects?onboarding=project"),
        ("source", "Add a trusted source", "Upload or sync source material into that project.", "/projects?onboarding=source"),
        ("research", "Run the first research task", "Produce a cited result grounded in the project sources.", "/research?onboarding=research"),
        ("approval", "Review a sensitive action", "Approve or reject one governed external action.", "/approvals?onboarding=approval"),
        ("schedule", "Create a scheduled task", "Set a recurring workflow and verify its next run.", "/workflows?onboarding=schedule"),
    ]
    steps = [
        {
            "id": step_id,
            "label": label,
            "description": description,
            "href": href,
            "complete": counts[step_id] > 0,
            "evidence_count": counts[step_id],
        }
        for step_id, label, description, href in definitions
    ]
    complete = sum(1 for step in steps if step["complete"])
    return {"complete": complete, "total": len(steps), "steps": steps}


@router.get("/runtime-health")
async def get_runtime_health(
    refresh: bool = Query(False),
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    """Return authenticated, redacted platform readiness for this workspace.

    Every member can see whether required services are available so an optional
    provider outage never masquerades as a broken workspace. Only admins can
    force external credential verification or receive remediation instructions.
    """

    await permissions.check(member, "read_settings", member.organization_id)
    is_admin = member.role in ADMIN_ROLES
    if refresh and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only organization administrators can refresh provider verification.",
        )
    report = await runtime_health.build_runtime_health_report(
        can_admin=is_admin,
        refresh_providers=refresh,
    )
    if refresh:
        await audit.log(
            "runtime_health_refreshed",
            member.id,
            "settings.runtime_health",
            organization_id=member.organization_id,
            resource_type="organization",
            resource_id=member.organization_id,
            payload={"status": report["status"]},
        )
    return report


@router.post("/onboarding/complete")
async def complete_onboarding(member: Member = Depends(get_current_member)) -> dict:
    require_admin(member)
    readiness = await runtime_health.build_runtime_health_report(
        can_admin=True,
        refresh_providers=True,
    )
    if not readiness["can_complete_onboarding"]:
        labels = ", ".join(item["label"] for item in readiness["blockers"])
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "Required runtime services are not ready. Resolve the listed "
                    f"blockers and check again: {labels}."
                ),
                "code": "runtime_not_ready",
                "blockers": readiness["blockers"],
            },
        )
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(update(organizations).where(
            organizations.c.id == member.organization_id).values(onboarding_state="complete"))
    await audit.log("onboarding_completed", member.id, "settings.onboarding_complete",
                    organization_id=member.organization_id, resource_type="organization",
                    resource_id=member.organization_id)
    return {"state": "complete"}


@router.get("/plan")
async def get_plan(member: Member = Depends(get_current_member)) -> dict[str, Any]:
    """Return the org's plan, its entitlements, and today's usage.

    Available to any authenticated member (no admin gate — everyone can see their plan tier).
    """
    # Resolve org plan from organizations table
    organizations = await reflect_table("organizations")
    members_table = await reflect_table("members")
    async with engine.begin() as conn:
        org_row = (
            await conn.execute(
                select(organizations.c.plan).where(organizations.c.id == member.organization_id)
            )
        ).first()
        seats_used = (
            await conn.execute(
                select(func.count()).select_from(members_table).where(
                    members_table.c.organization_id == member.organization_id
                )
            )
        ).scalar_one()
    plan = (org_row[0] if org_row and org_row[0] else None) or "trial"
    ent = get_entitlements(plan)
    ent_dict = dataclasses.asdict(ent)
    ent_dict["features"] = sorted(ent_dict["features"])

    # Usage today from governance (tokens + cost)
    try:
        summary = await usage_summary(member.organization_id)
        tokens_today = summary.get("tokens", {}).get("tokens_today", 0)
        cost_today_usd = summary.get("cost", {}).get("cost_today_usd", 0.0)
    except Exception:
        tokens_today = 0
        cost_today_usd = 0.0

    return {
        "plan": plan,
        "entitlements": ent_dict,
        "usage": {
            "seats_used": int(seats_used),
            "tokens_today": tokens_today,
            "cost_today_usd": cost_today_usd,
        },
    }


@router.patch("/{section}")
async def update_section(section: str, req: SettingsPatch, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    if section not in DEFAULTS:
        raise HTTPException(status_code=404, detail="Unknown settings section")
    if section in ADMIN_SECTIONS:
        require_admin(member)
    if section == "general" and member.role not in ADMIN_ROLES:
        personal_keys = {
            "theme",
            "language",
            "time_zone",
            "date_time_format",
            "default_landing_page",
            "accent",
        }
        personal = {key: value for key, value in req.values.items() if key in personal_keys}
        _validate_section(section, personal)
        profile = await save_settings_doc(
            member, "profile", {"ui_preferences": personal}, scope="user", scope_id=member.id
        )
        general = await get_settings_doc(member, "general")
        general.update(profile.get("ui_preferences") or {})
        return {"section": section, "values": general}
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
                select(members).where(
                    members.c.id == member_id,
                    members.c.organization_id == member.organization_id,
                ).with_for_update()
            )
        ).mappings().first()
        if not target:
            raise HTTPException(status_code=404, detail="Member not found")
        if req.role == "owner" and target["role"] != "owner":
            raise HTTPException(
                status_code=409,
                detail="Use the confirmation-protected ownership transfer action",
            )
        owner_count = (
            await conn.execute(
                select(func.count()).select_from(members).where(
                    members.c.organization_id == member.organization_id,
                    members.c.role == "owner",
                    members.c.status == "active",
                )
            )
        ).scalar_one()
        if target["role"] == "owner" and req.role != "owner":
            if member.role != "owner":
                raise HTTPException(status_code=403, detail="Only an owner can change another owner's role")
            if owner_count <= 1:
                raise HTTPException(status_code=409, detail="Cannot demote the last organization owner")
        await conn.execute(update(members).where(members.c.id == member_id).values(role=req.role))
    await permissions.sync_org_membership(
        member_id, member.organization_id, role=req.role, active=True
    )
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

    # Seat-cap enforcement (W4): count active members + pending invitations and
    # reject before creating a new invitation if the plan limit is reached.
    organizations = await reflect_table("organizations")
    invitations_table = await reflect_table("invitations")
    async with engine.begin() as conn:
        org_row = (
            await conn.execute(
                select(organizations.c.plan).where(organizations.c.id == member.organization_id)
            )
        ).first()
        plan = (org_row[0] if org_row else None) or "trial"
        member_count = (
            await conn.execute(
                select(func.count()).select_from(members).where(
                    members.c.organization_id == member.organization_id
                )
            )
        ).scalar_one()
        pending_count = (
            await conn.execute(
                select(func.count()).select_from(invitations_table).where(
                    invitations_table.c.organization_id == member.organization_id,
                    invitations_table.c.status == "pending",
                )
            )
        ).scalar_one()
    seats_used = member_count + pending_count
    entitlements = get_entitlements(plan)
    if seats_used >= entitlements.max_seats:
        raise HTTPException(
            status_code=402,
            detail=f"Seat limit reached for the {plan} plan",
        )

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
    api_keys = await reflect_table("organization_api_keys")
    async with engine.begin() as conn:
        target = (
            await conn.execute(
                select(members).where(
                    members.c.id == member_id,
                    members.c.organization_id == member.organization_id,
                    members.c.status == "active",
                ).with_for_update()
            )
        ).mappings().first()
        if not target:
            raise HTTPException(status_code=404, detail="Member not found")
        owner_count = (
            await conn.execute(
                select(func.count()).select_from(members).where(
                    members.c.organization_id == member.organization_id,
                    members.c.role == "owner",
                    members.c.status == "active",
                )
            )
        ).scalar_one()
        if target["role"] == "owner":
            if member.role != "owner":
                raise HTTPException(status_code=403, detail="Only an owner can deactivate another owner")
            if owner_count <= 1:
                raise HTTPException(status_code=409, detail="Cannot deactivate the last organization owner")
        await conn.execute(
            update(members).where(members.c.id == member_id).values(status="deactivated")
        )
        await conn.execute(
            update(api_keys)
            .where(
                api_keys.c.organization_id == member.organization_id,
                api_keys.c.created_by_member_id == member_id,
                api_keys.c.status == "active",
            )
            .values(
                status="revoked",
                revoked_at=func.now(),
                revoked_by=member.id,
                updated_at=func.now(),
            )
        )
    await permissions.sync_org_membership(
        member_id, member.organization_id, role=str(target["role"]), active=False
    )
    await audit.log("settings_change", member.id, "settings.members.remove", organization_id=member.organization_id, resource_type="members", resource_id=member_id)
    return {"id": member_id, "removed": True, "status": "deactivated"}


@router.post("/memory/purge")
async def purge_memory(req: MemoryPurgeRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    require_admin(member)
    if req.confirmation != "PURGE MEMORY":
        raise HTTPException(status_code=400, detail='Type "PURGE MEMORY" to confirm')
    return await retention.soft_delete_all_memory(
        member.organization_id, actor_id=member.id
    )


@router.get("/retention/holds")
async def get_retention_holds(
    active_only: bool = Query(True),
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    require_admin(member)
    return {
        "holds": await retention.list_holds(
            member.organization_id, active_only=active_only
        )
    }


@router.post("/retention/holds")
async def add_retention_hold(
    req: RetentionHoldCreate,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    require_admin(member)
    resource_id = req.resource_id.strip()
    reason = req.reason.strip()
    if len(reason) < 5:
        raise HTTPException(
            status_code=400, detail="Retention hold reason must be at least 5 characters"
        )
    try:
        return await retention.create_hold(
            org_id=member.organization_id,
            region=member.region,
            resource_type=req.resource_type,
            resource_id=resource_id,
            reason=reason,
            actor_id=member.id,
        )
    except retention.RetentionResourceNotFound:
        # Non-enumerating across tenants: a foreign id and a missing id are the
        # same response.
        raise HTTPException(
            status_code=404, detail="Retention resource not found"
        ) from None


@router.delete("/retention/holds/{hold_id}")
async def remove_retention_hold(
    hold_id: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    require_admin(member)
    if not await retention.release_hold(member.organization_id, hold_id, member.id):
        raise HTTPException(status_code=404, detail="Active retention hold not found")
    return {"id": hold_id, "released": True}


@router.post("/retention/run")
async def run_retention_now(
    req: RetentionRunRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    require_admin(member)
    if not req.dry_run and req.confirmation != "RUN RETENTION":
        raise HTTPException(
            status_code=400,
            detail='Type "RUN RETENTION" to execute irreversible retention',
        )
    return await retention.run_retention(
        member.organization_id,
        dry_run=req.dry_run,
        actor_id=member.id,
    )


@router.get("/memory/export.json")
async def export_memory(member: Member = Depends(get_current_member)) -> JSONResponse:
    require_admin(member)
    # This is explicitly an organization export. Never include the requesting
    # admin's personal/restricted memories (or private project memories) merely
    # because they are otherwise visible in that member's control center.
    items = [
        item
        for item in await export_memories(member)
        if item.get("scope") == "org" and item.get("scope_id") == member.organization_id
    ]
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


_AUDIT_EXPORT_COLUMNS = [
    "id", "created_at", "actor_id", "event_type", "action", "resource_type", "resource_id", "decision",
]
_AUDIT_EXPORT_BATCH = 1000


def _parse_audit_ts(value: str | None, field: str) -> datetime | None:
    """Parse an ISO-8601 timestamp from a query param; trailing 'Z' allowed."""
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {field} timestamp: {value!r}")


def _audit_select(
    audit_log,
    *,
    organization_id: str,
    actor: str | None = None,
    action: str | None = None,
    query: str | None = None,
    event_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
):
    """Build the shared, tenant-scoped, filtered audit query used by list + export."""
    stmt = select(audit_log).where(audit_log.c.organization_id == organization_id)
    if actor:
        stmt = stmt.where(audit_log.c.actor_id == actor)
    if action:
        stmt = stmt.where(audit_log.c.action == action)
    if event_type:
        stmt = stmt.where(audit_log.c.event_type == event_type)
    if query:
        stmt = stmt.where(audit_log.c.action.ilike(f"%{query}%"))
    if since is not None:
        stmt = stmt.where(audit_log.c.created_at >= since)  # inclusive lower
    if until is not None:
        stmt = stmt.where(audit_log.c.created_at < until)  # exclusive upper
    return stmt


@router.get("/audit")
async def list_audit(
    actor: str | None = None,
    action: str | None = None,
    query: str | None = None,
    event_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_audit_log", member.organization_id)
    audit_log = await reflect_table("audit_log")
    stmt = _audit_select(
        audit_log,
        organization_id=member.organization_id,
        actor=actor,
        action=action,
        query=query,
        event_type=event_type,
        since=_parse_audit_ts(since, "since"),
        until=_parse_audit_ts(until, "until"),
    )
    stmt = stmt.order_by(audit_log.c.created_at.desc()).limit(limit).offset(offset)
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(row) for row in rows]


@router.post("/authz/reconcile")
async def reconcile_authz(member: Member = Depends(get_current_member)) -> dict[str, int]:
    """Backfill OpenFGA relationship tuples from the DB for this org (W2.5).

    Idempotent — safe to run multiple times. No-ops if OpenFGA is not
    configured. Requires admin or owner role.
    """
    require_admin(member)
    return await permissions.reconcile_org_tuples(member.organization_id)


def _audit_json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, _uuid.UUID):
        return str(value)
    return str(value)


def _redacted_audit_row(row: Any) -> dict[str, Any]:
    """Redact both current and historical rows at the export boundary."""
    from core.audit_redaction import redact

    return redact(dict(row))


@router.get("/audit/export")
async def export_audit(
    format: str = Query(default="csv", pattern="^(csv|json)$"),
    actor: str | None = None,
    action: str | None = None,
    query: str | None = None,
    event_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    member: Member = Depends(get_current_member),
) -> StreamingResponse:
    """Compliance-grade audit export (W5.1).

    Streams the **complete** filtered audit trail for the member's org — no
    silent row cap — in CSV or JSON, with a manifest (count, range, filters,
    generated_at, org, generated_by) that proves completeness. The export is
    itself admin-gated and audited. ``audit_log`` is never mutated (RULE 6).
    """
    await permissions.check(member, "export_audit_log", member.organization_id)
    since_dt = _parse_audit_ts(since, "since")
    until_dt = _parse_audit_ts(until, "until")
    audit_log = await reflect_table("audit_log")
    base = _audit_select(
        audit_log,
        organization_id=member.organization_id,
        actor=actor,
        action=action,
        query=query,
        event_type=event_type,
        since=since_dt,
        until=until_dt,
    )
    # Complete, deterministic order (oldest→newest) for reproducible exports.
    ordered = base.order_by(audit_log.c.created_at.asc(), audit_log.c.id.asc())

    # Known up front so the manifest/headers can assert completeness without
    # buffering every row in memory.
    async with engine.begin() as conn:
        total = (await conn.execute(
            select(func.count()).select_from(base.subquery())
        )).scalar_one()

    generated_at = datetime.now(timezone.utc)
    filters = {
        "actor": actor,
        "action": action,
        "query": query,
        "event_type": event_type,
        "since": since_dt.isoformat() if since_dt else None,
        "until": until_dt.isoformat() if until_dt else None,
    }
    manifest = {
        "organization_id": member.organization_id,
        "generated_by": member.id,
        "generated_at": generated_at.isoformat(),
        "count": int(total),
        "filters": filters,
        "format": format,
    }

    # Record the export itself before streaming, so it is captured even if the
    # client disconnects mid-download.
    await audit.log(
        "compliance",
        member.id,
        "export_audit_log",
        organization_id=member.organization_id,
        resource_type="audit_log",
        resource_id=member.organization_id,
        payload={"count": int(total), "filters": filters, "format": format},
    )

    async def _iter_rows():
        offset = 0
        while True:
            async with engine.begin() as conn:
                rows = (await conn.execute(
                    ordered.limit(_AUDIT_EXPORT_BATCH).offset(offset)
                )).mappings().all()
            if not rows:
                return
            for row in rows:
                yield _redacted_audit_row(row)
            if len(rows) < _AUDIT_EXPORT_BATCH:
                return
            offset += _AUDIT_EXPORT_BATCH

    if format == "json":
        async def stream_json():
            yield '{"manifest": ' + json.dumps(manifest) + ', "records": ['
            first = True
            async for row in _iter_rows():
                yield ("" if first else ",") + json.dumps(row, default=_audit_json_default)
                first = False
            yield "]}"

        return StreamingResponse(
            stream_json(),
            media_type="application/json",
            headers={
                "Content-Disposition": "attachment; filename=chronos-audit.json",
                "X-Chronos-Audit-Export-Count": str(total),
            },
        )

    async def stream_csv():
        handle = io.StringIO()
        writer = csv.DictWriter(handle, fieldnames=_AUDIT_EXPORT_COLUMNS)
        writer.writeheader()
        yield handle.getvalue()
        async for row in _iter_rows():
            handle.seek(0)
            handle.truncate(0)
            writer.writerow({key: row.get(key) for key in _AUDIT_EXPORT_COLUMNS})
            yield handle.getvalue()

    return StreamingResponse(
        stream_csv(),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=chronos-audit.csv",
            "X-Chronos-Audit-Export-Count": str(total),
            "X-Chronos-Audit-Export-Generated-At": generated_at.isoformat(),
        },
    )


@router.get("/audit/export.csv")
async def export_audit_csv(
    actor: str | None = None,
    action: str | None = None,
    query: str | None = None,
    event_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    member: Member = Depends(get_current_member),
) -> StreamingResponse:
    """Back-compat alias for the existing web link; now complete (no 500 cap)."""
    return await export_audit(
        format="csv",
        actor=actor,
        action=action,
        query=query,
        event_type=event_type,
        since=since,
        until=until,
        member=member,
    )
