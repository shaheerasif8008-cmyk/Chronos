"""Admin console (W5.4).

A single governed surface that aggregates the org's admin-relevant state
(members/roles, connectors, audit volume, access-control posture, notification
delivery) so the console has one authoritative landing summary instead of
scattering counts across a dozen endpoints. Admin-gated and audited.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from core import audit, authz, notification_delivery, permissions
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from core.settings_store import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])


async def _count(table_name: str, org_id: str, *extra) -> int:
    table = await reflect_table(table_name)
    stmt = select(func.count()).select_from(table).where(table.c.organization_id == org_id, *extra)
    async with engine.begin() as conn:
        return int((await conn.execute(stmt)).scalar_one())


async def _members_by_role(org_id: str) -> dict[str, int]:
    members = await reflect_table("members")
    stmt = select(members.c.role, func.count()).where(
        members.c.organization_id == org_id
    ).group_by(members.c.role)
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).all()
    return {role: int(n) for role, n in rows}


@router.get("/overview")
async def admin_overview(member: Member = Depends(get_current_member)) -> dict[str, Any]:
    """Aggregated admin landing summary. Admin-only, audited."""
    require_admin(member)
    await permissions.check(member, "view_admin_console", member.organization_id)
    org_id = member.organization_id

    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        org_row = (await conn.execute(
            select(organizations).where(organizations.c.id == org_id)
        )).mappings().first()
    org = dict(org_row) if org_row else {}

    by_role = await _members_by_role(org_id)
    connectors = await reflect_table("connectors")
    pending_approvals = await _count("approvals", org_id, (await reflect_table("approvals")).c.status == "pending")

    await audit.log(
        "admin", str(member.id), "view_admin_console",
        organization_id=org_id, resource_type="organization", resource_id=org_id,
    )

    return {
        "organization": {
            "id": org.get("id"),
            "name": org.get("name"),
            "slug": org.get("slug"),
            "plan": org.get("plan"),
            "region": org.get("region"),
        },
        "members": {"total": sum(by_role.values()), "by_role": by_role},
        "connectors": {"active": await _count("connectors", org_id, connectors.c.status == "active")},
        "approvals": {"pending": pending_approvals},
        "audit": {"total_events": await _count("audit_log", org_id)},
        "notifications": {"unread_org_wide": await _count(
            "notifications", org_id,
            (await reflect_table("notifications")).c.member_id.is_(None),
            (await reflect_table("notifications")).c.read_at.is_(None),
        )},
        "governance": {
            "openfga_enabled": authz.is_enabled(),
            "sso_configured": bool(settings.cognito_user_pool_id),
            "email_delivery_configured": notification_delivery.email_is_configured(),
        },
    }
