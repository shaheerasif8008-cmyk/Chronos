"""In-app notification feed (W5.2).

Durable, tenant-scoped notification records plus the read/dismiss lifecycle.
Emission is the single entry point ``emit(...)`` — runtime/event sites call it
best-effort and it is gated by the per-org notification settings that already
exist in ``settings_store.DEFAULTS["notifications"]`` (previously inert).

A notification with ``member_id is None`` is org-wide (visible to every member
of the org, e.g. an approval awaiting any admin); a set ``member_id`` targets a
single recipient. ``audit_log`` records every creation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, insert, or_, select, update

from core import audit
from core.config import settings as app_settings
from core.db import engine, reflect_table
from core.settings_store import DEFAULTS, deep_merge

# Notification type -> the org-settings toggle that gates it. Unmapped types are
# always allowed (still subject to the master ``in_app`` switch).
_SETTING_GATE = {
    "approval_request": "approval_request_alerts",
    "approval_decision": "approval_request_alerts",
    "task_failure": "runtime_failure_alerts",
    "task_completion": "task_completion_alerts",
    "security": "security_alerts",
}

_VALID_SEVERITY = {"info", "success", "warning", "critical"}


async def _notification_settings(organization_id: str) -> dict[str, Any]:
    table = await reflect_table("settings_documents")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table.c["values"]).where(
                    table.c.organization_id == organization_id,
                    table.c.scope == "org",
                    table.c.scope_id == organization_id,
                    table.c.section == "notifications",
                )
            )
        ).first()
    stored = dict(row[0] or {}) if row else {}
    return deep_merge(DEFAULTS.get("notifications", {}), stored)


async def is_enabled(organization_id: str, ntype: str) -> bool:
    """Whether in-app notifications of ``ntype`` are enabled for the org."""
    settings = await _notification_settings(organization_id)
    if settings.get("in_app") is False:
        return False
    gate = _SETTING_GATE.get(ntype)
    if gate is None:
        return True
    return settings.get(gate, True) is not False


async def emit(
    *,
    organization_id: str,
    type: str,
    title: str,
    body: str | None = None,
    severity: str = "info",
    member_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    created_by: str = "chronos",
) -> str | None:
    """Create a notification if the org has this type enabled. Returns the id or
    None when suppressed. Never raises into the caller's flow on a settings miss —
    callers may still wrap this best-effort, but the happy path is exception-free.
    """
    if severity not in _VALID_SEVERITY:
        severity = "info"
    if not await is_enabled(organization_id, type):
        return None
    table = await reflect_table("notifications")
    async with engine.begin() as conn:
        row = await conn.execute(
            insert(table)
            .values(
                organization_id=organization_id,
                region=app_settings.region,
                member_id=member_id,
                type=type,
                title=title,
                body=body,
                severity=severity,
                resource_type=resource_type,
                resource_id=resource_id,
                created_by=created_by,
            )
            .returning(table.c.id)
        )
        nid = str(row.scalar_one())
    await audit.log(
        "notification",
        created_by,
        "notification_created",
        organization_id=organization_id,
        resource_type="notification",
        resource_id=nid,
        payload={"type": type, "severity": severity, "member_id": member_id},
    )
    return nid


def _visible_to(table, organization_id: str, member_id: str):
    """A notification is visible to a member if it is org-wide (member_id NULL) or
    explicitly addressed to them."""
    return and_(
        table.c.organization_id == organization_id,
        or_(table.c.member_id.is_(None), table.c.member_id == member_id),
    )


async def list_for(
    organization_id: str,
    member_id: str,
    *,
    include_dismissed: bool = False,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    table = await reflect_table("notifications")
    stmt = select(table).where(_visible_to(table, organization_id, member_id))
    if not include_dismissed:
        stmt = stmt.where(table.c.dismissed_at.is_(None))
    if unread_only:
        stmt = stmt.where(table.c.read_at.is_(None))
    stmt = stmt.order_by(table.c.created_at.desc()).limit(limit).offset(offset)
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


async def unread_count(organization_id: str, member_id: str) -> int:
    table = await reflect_table("notifications")
    stmt = select(func.count()).select_from(table).where(
        _visible_to(table, organization_id, member_id),
        table.c.read_at.is_(None),
        table.c.dismissed_at.is_(None),
    )
    async with engine.begin() as conn:
        return int((await conn.execute(stmt)).scalar_one())


async def mark_read(
    organization_id: str, member_id: str, ids: list[str] | None = None
) -> int:
    """Mark the given notifications read (or all visible unread ones when ids is
    None). Only affects rows visible to the member; tenant-scoped."""
    table = await reflect_table("notifications")
    now = datetime.now(timezone.utc)
    cond = and_(_visible_to(table, organization_id, member_id), table.c.read_at.is_(None))
    if ids is not None:
        cond = and_(cond, table.c.id.in_(ids))
    async with engine.begin() as conn:
        result = await conn.execute(update(table).where(cond).values(read_at=now))
    return result.rowcount or 0


async def dismiss(organization_id: str, member_id: str, ids: list[str]) -> int:
    table = await reflect_table("notifications")
    now = datetime.now(timezone.utc)
    cond = and_(
        _visible_to(table, organization_id, member_id),
        table.c.dismissed_at.is_(None),
        table.c.id.in_(ids),
    )
    async with engine.begin() as conn:
        result = await conn.execute(update(table).where(cond).values(dismissed_at=now))
    return result.rowcount or 0
