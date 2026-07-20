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

# Settings documents are intentionally schema-light JSON, but these two
# sections feed enforcement code and must not accumulate controls that merely
# *look* operational.  The allowlists are used on both read and write paths so
# stale values from earlier builds do not reappear in the admin UI.
RUNTIME_SETTING_KEYS = {
    "token_budget_daily",
    "cost_budget_daily_usd",
    "request_rate_per_minute",
    "connector_rate_per_minute",
    "max_task_queue_size",
}
MEMORY_SETTING_KEYS = {
    "retention_enabled",
    "retention_days",
    "deleted_retention_days",
    "deleted_artifact_retention_days",
}
AI_EMPLOYEE_SETTING_KEYS = {"max_concurrent_runtimes"}
APPROVAL_SETTING_KEYS: set[str] = set()
GENERAL_SETTING_KEYS = {"theme", "accent"}
PROFILE_SETTING_KEYS = {"ui_preferences"}
DEVELOPER_SETTING_KEYS: set[str] = set()
SECTION_SETTING_KEYS: dict[str, set[str]] = {
    "general": GENERAL_SETTING_KEYS,
    "profile": PROFILE_SETTING_KEYS,
    "runtime": RUNTIME_SETTING_KEYS,
    "memory": MEMORY_SETTING_KEYS,
    "ai_employee": AI_EMPLOYEE_SETTING_KEYS,
    # Approval enforcement is owned by the broker risk floor, connector
    # policies, and the ratified autonomy APIs. Legacy JSON controls were never
    # read on the execution path, so hide and discard them instead of presenting
    # a dangerous settings illusion.
    "approval": APPROVAL_SETTING_KEYS,
    "developer": DEVELOPER_SETTING_KEYS,
}


DEFAULTS: dict[str, dict[str, Any]] = {
    "general": {
        "theme": "system",
        "accent": "coral",
    },
    "profile": {"ui_preferences": {}},
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
        "max_concurrent_runtimes": 3,
    },
    "runtime": {
        "max_task_queue_size": 100,
        # token_budget_daily and cost_budget_daily_usd are intentionally absent from DEFAULTS
        # so that save_settings_doc never writes them into settings_documents unless an admin
        # explicitly overrides them. governance_config falls back to plan entitlements when
        # these keys are absent from the stored doc.
        "request_rate_per_minute": 60,
        "connector_rate_per_minute": 60,
    },
    "memory": {
        "retention_enabled": True,
        "retention_days": 365,
        # Active memories are soft-deleted at ``retention_days``.  A separate
        # grace period preserves recovery time before irreversible erasure.
        "deleted_retention_days": 30,
        # User-deleted artifacts are retained briefly before their object bytes
        # and metadata are irreversibly removed.
        "deleted_artifact_retention_days": 30,
    },
    "tool_settings": {
        "browser": {"enabled": True, "approval_required": False, "risk": "low"},
        "gmail": {"enabled": True, "approval_required": True, "risk": "high"},
        "chat_history": {"enabled": True, "approval_required": False, "risk": "low"},
    },
    "approval": {},
    "notifications": {
        "email": False,
        "slack": False,
        "teams": False,
        "in_app": True,
        "desktop": True,
        "runtime_failure_alerts": True,
        "approval_request_alerts": True,
        "task_completion_alerts": True,
        "weekly_digest": False,
        "security_alerts": True,
    },
    "developer": {},
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
    merged = deep_merge(DEFAULTS.get(section, {}), dict(row[0] or {}) if row else {})
    allowed = SECTION_SETTING_KEYS.get(section)
    if allowed is not None:
        return {key: merged[key] for key in allowed if key in merged}
    return merged


async def save_settings_doc(
    member: Member,
    section: str,
    values: dict[str, Any],
    *,
    scope: str | None = None,
    scope_id: str | None = None,
) -> dict[str, Any]:
    allowed = SECTION_SETTING_KEYS.get(section)
    if allowed is not None:
        values = {key: value for key, value in values.items() if key in allowed}
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


# Per-tool permissions (Anthropic-style connector tool permissions).
# "default" keeps the broker's normal governance; "always_allow" skips the
# settings/autonomy gate (never the hard approval floor); "require_approval"
# forces an approval record; "blocked" removes the tool entirely.
VALID_TOOL_PERMISSIONS: frozenset[str] = frozenset(
    {"default", "always_allow", "require_approval", "blocked"}
)


async def tool_permissions(org_id: str) -> dict[str, str]:
    """Return the org's per-tool permission overrides keyed by broker tool name."""
    member = Member(id="system", organization_id=org_id, region=settings.region, email="system@local", role="admin")
    doc = await get_settings_doc(member, "tool_permissions", scope="org", scope_id=org_id)
    return {
        str(tool): str(perm)
        for tool, perm in doc.items()
        if isinstance(perm, str) and perm in VALID_TOOL_PERMISSIONS
    }


async def set_tool_permission(member: Member, tool: str, permission: str) -> dict[str, str]:
    """Persist one per-tool permission override for the member's org."""
    if permission not in VALID_TOOL_PERMISSIONS:
        raise ValueError(f"Invalid tool permission: {permission}")
    await save_settings_doc(member, "tool_permissions", {tool: permission}, scope="org")
    return await tool_permissions(member.organization_id)


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
