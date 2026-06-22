from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.sql import func

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member

ADMIN_ROLES = {"owner", "admin"}
ROLE_ORDER = ["owner", "admin", "manager", "operator", "viewer"]


DEFAULTS: dict[str, dict[str, Any]] = {
    "general": {
        "workspace_name": "Chronos workspace",
        "workspace_description": "Local Chronos operations workspace.",
        "workspace_icon": "C",
        "default_landing_page": "chat",
        "time_zone": "America/New_York",
        "date_time_format": "MMM d, yyyy h:mm a",
        "language": "en-US",
        "theme": "system",
        "notifications": {"in_app": True, "email": False, "approvals": True, "runtime_failures": True},
    },
    "profile": {
        "display_name": "",
        "profile_avatar": "",
        "personal_preferences": "",
        "ai_interaction_style": "balanced",
        "preferred_response_length": "medium",
        "citation_detail_level": "standard",
    },
    "organization": {
        "organization_name": "",
        "logo": "",
        "domain": "",
        "plan": "trial",
        "default_workspace_creation": "admins",
    },
    "permissions": {
        "roles": {
            "owner": {"workspace": "allow", "employee": "allow", "tools": "allow", "approvals": "allow", "memory": "allow", "audit": "allow"},
            "admin": {"workspace": "allow", "employee": "allow", "tools": "allow", "approvals": "allow", "memory": "allow", "audit": "allow"},
            "manager": {"workspace": "deny", "employee": "allow", "tools": "approval_required", "approvals": "allow", "memory": "allow", "audit": "deny"},
            "operator": {"workspace": "deny", "employee": "deny", "tools": "approval_required", "approvals": "allow", "memory": "allow", "audit": "deny"},
            "viewer": {"workspace": "deny", "employee": "deny", "tools": "deny", "approvals": "deny", "memory": "deny", "audit": "deny"},
        }
    },
    "ai_employee": {
        "creation_policy": "admins_and_managers",
        "memory_scope": "workspace",
        "tool_access_mode": "approval_required",
        "runtime_auto_start": True,
        "runtime_idle_timeout_minutes": 30,
        "max_concurrent_runtimes": 3,
        "max_sub_agent_depth": 3,
        "sub_agent_spawning": True,
        "approval_threshold_depth": 2,
    },
    "runtime": {
        "runtime_mode": "local",
        "isolation": "process",
        "heartbeat_interval_seconds": 30,
        "restart_policy": "on_failure",
        "log_retention_days": 14,
        "max_task_queue_size": 100,
        "failure_recovery": "resume",
        "token_budget_daily": 100000,
        "cost_budget_daily_usd": 10,
        "request_rate_per_minute": 60,
        "connector_rate_per_minute": 60,
    },
    "memory": {
        "workspace_memory": True,
        "employee_memory": True,
        "user_memory": True,
        "retention_days": 365,
        "review_required": False,
        "auto_save": True,
        "sensitive_detection": False,
    },
    "tool_settings": {
        "browser": {"enabled": True, "approval_required": False, "risk": "low"},
        "gmail": {"enabled": True, "approval_required": True, "risk": "high"},
        "chat_history": {"enabled": True, "approval_required": False, "risk": "low"},
    },
    "approval": {
        "mode": "manual",
        "thresholds": {"low": "auto", "medium": "manual", "high": "strict"},
        "rules": [
            {"id": "gmail-draft", "target": "gmail.draft", "decision": "approval_required"},
            {"id": "external-send", "target": "gmail.send", "decision": "blocked"},
        ],
    },
    "notifications": {
        "email": False,
        "in_app": True,
        "runtime_failure_alerts": True,
        "approval_request_alerts": True,
        "task_completion_alerts": True,
        "weekly_digest": False,
        "security_alerts": True,
    },
    "developer": {
        "feature_flags": {},
        "api_mode": "local",
        "debug_logging": False,
        "experimental_features": False,
    },
    "response_format": {
        "verbosity": "detailed",
    },
}


def require_admin(member: Member) -> None:
    from fastapi import HTTPException

    if member.role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin role required")


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


async def get_settings_doc(member: Member, section: str, *, scope: str | None = None, scope_id: str | None = None) -> dict[str, Any]:
    target_scope = scope or ("user" if section == "profile" else "org")
    target_scope_id = scope_id or (member.id if target_scope == "user" else member.organization_id)
    table = await reflect_table("settings_documents")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table.c["values"]).where(
                    table.c.organization_id == member.organization_id,
                    table.c.scope == target_scope,
                    table.c.scope_id == target_scope_id,
                    table.c.section == section,
                )
            )
        ).first()
    return deep_merge(DEFAULTS.get(section, {}), dict(row[0] or {}) if row else {})


async def save_settings_doc(
    member: Member,
    section: str,
    values: dict[str, Any],
    *,
    scope: str | None = None,
    scope_id: str | None = None,
) -> dict[str, Any]:
    target_scope = scope or ("user" if section == "profile" else "org")
    target_scope_id = scope_id or (member.id if target_scope == "user" else member.organization_id)
    current = await get_settings_doc(member, section, scope=target_scope, scope_id=target_scope_id)
    next_values = deep_merge(current, values)
    table = await reflect_table("settings_documents")
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                select(table.c.id, table.c["values"]).where(
                    table.c.organization_id == member.organization_id,
                    table.c.scope == target_scope,
                    table.c.scope_id == target_scope_id,
                    table.c.section == section,
                )
            )
        ).mappings().first()
        if existing:
            await conn.execute(
                update(table)
                .where(table.c.id == existing["id"])
                .values(values=next_values, updated_by=member.id, updated_at=func.now())
            )
            resource_id = existing["id"]
            before = dict(existing["values"] or {})
        else:
            result = await conn.execute(
                insert(table)
                .values(
                    organization_id=member.organization_id,
                    region=member.region,
                    scope=target_scope,
                    scope_id=target_scope_id,
                    section=section,
                    values=next_values,
                    updated_by=member.id,
                )
                .returning(table.c.id)
            )
            resource_id = str(result.scalar_one())
            before = {}
    await audit.log(
        "settings_change",
        member.id,
        f"settings.{section}.update",
        organization_id=member.organization_id,
        resource_type="settings",
        resource_id=resource_id,
        payload={"section": section, "scope": target_scope, "before": before, "after": next_values},
    )
    return next_values


async def tool_policy(org_id: str, provider: str) -> dict[str, Any]:
    member = Member(id="system", organization_id=org_id, region=settings.region, email="system@local", role="admin")
    policies = await get_settings_doc(member, "tool_settings", scope="org", scope_id=org_id)
    return dict(policies.get(provider, {"enabled": True, "approval_required": False, "risk": "unknown"}))


# Autonomy levels a workspace can run at. ``full_auto`` collapses settings-policy
# approval gates (never the hard floor in tool_broker). Default is ``supervised``.
AUTONOMY_LEVELS: frozenset[str] = frozenset({"supervised", "full_auto"})


async def workspace_autonomy(org_id: str, workspace_id: str | None) -> str:
    """Return the autonomy level for a workspace, defaulting to ``supervised``.

    Stored as a settings document (section ``autonomy``, scope ``workspace``) so
    it honors the per-workspace requirement without a dedicated table. When no
    workspace is supplied the ``default`` workspace document is used.
    """
    member = Member(id="system", organization_id=org_id, region=settings.region, email="system@local", role="admin")
    try:
        doc = await get_settings_doc(
            member, "autonomy", scope="workspace", scope_id=str(workspace_id or "default")
        )
    except Exception:
        return "supervised"
    level = str(doc.get("level") or "supervised")
    return level if level in AUTONOMY_LEVELS else "supervised"
