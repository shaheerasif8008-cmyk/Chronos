"""In-app notification feed (W5.2).

Durable, tenant-scoped notification records plus the read/dismiss lifecycle.
Emission is the single entry point ``emit(...)`` — runtime/event sites call it
best-effort and it is gated by the per-org notification settings that already
exist in ``settings_store.DEFAULTS["notifications"]`` (previously inert).

A notification with ``member_id is None`` is org-wide (visible to every member
of the org, e.g. an approval awaiting any admin); a set ``member_id`` targets a
single recipient. Read and dismiss state lives in ``notification_receipts`` so
one member can never mutate another member's feed state. ``audit_log`` records
every creation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, insert, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

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
    # Native desktop delivery is best-effort and uses only the narrow signed
    # title/body/category command. The durable in-app row above remains the
    # source of truth even when no paired device is online.
    try:
        notification_settings = await _notification_settings(organization_id)
        if notification_settings.get("desktop", True) is not False:
            from core.desktop_bridge import desktop_bridge

            await desktop_bridge.enqueue_notification(
                organization_id=organization_id,
                member_id=member_id,
                title=title,
                body=body,
                category=severity,
            )
    except Exception:
        pass
    return nid


def _visible_to(table, organization_id: str, member_id: str):
    """A notification is visible to a member if it is org-wide (member_id NULL) or
    explicitly addressed to them."""
    return and_(
        table.c.organization_id == organization_id,
        or_(table.c.member_id.is_(None), table.c.member_id == member_id),
    )


def _receipt_join(table, receipts, organization_id: str, member_id: str):
    """Join a member's receipt without ever crossing a tenant boundary."""
    return and_(
        receipts.c.organization_id == organization_id,
        receipts.c.notification_id == table.c.id,
        receipts.c.member_id == member_id,
    )


def _feed_columns(table, receipts):
    """Keep the public response shape while sourcing state from the receipt.

    ``notifications.read_at`` and ``notifications.dismissed_at`` remain in the
    schema for a non-breaking migration, but are deprecated and intentionally
    ignored by all member-facing paths.
    """
    return [
        *(column for column in table.c if column.name not in {"read_at", "dismissed_at"}),
        receipts.c.read_at.label("read_at"),
        receipts.c.dismissed_at.label("dismissed_at"),
    ]


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
    receipts = await reflect_table("notification_receipts")
    join = table.outerjoin(receipts, _receipt_join(table, receipts, organization_id, member_id))
    stmt = select(*_feed_columns(table, receipts)).select_from(join).where(
        _visible_to(table, organization_id, member_id)
    )
    if not include_dismissed:
        stmt = stmt.where(receipts.c.dismissed_at.is_(None))
    if unread_only:
        stmt = stmt.where(receipts.c.read_at.is_(None))
    stmt = stmt.order_by(table.c.created_at.desc()).limit(limit).offset(offset)
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


async def unread_count(organization_id: str, member_id: str) -> int:
    table = await reflect_table("notifications")
    receipts = await reflect_table("notification_receipts")
    join = table.outerjoin(receipts, _receipt_join(table, receipts, organization_id, member_id))
    stmt = select(func.count()).select_from(join).where(
        _visible_to(table, organization_id, member_id),
        receipts.c.read_at.is_(None),
        receipts.c.dismissed_at.is_(None),
    )
    async with engine.begin() as conn:
        return int((await conn.execute(stmt)).scalar_one())


async def unread_org_wide_count(organization_id: str, member_id: str) -> int:
    """Count org-wide notifications unread by one authenticated member."""
    table = await reflect_table("notifications")
    receipts = await reflect_table("notification_receipts")
    join = table.outerjoin(receipts, _receipt_join(table, receipts, organization_id, member_id))
    stmt = select(func.count()).select_from(join).where(
        table.c.organization_id == organization_id,
        table.c.member_id.is_(None),
        receipts.c.read_at.is_(None),
        receipts.c.dismissed_at.is_(None),
    )
    async with engine.begin() as conn:
        return int((await conn.execute(stmt)).scalar_one())


async def _set_receipt_timestamp(
    organization_id: str,
    member_id: str,
    field: str,
    ids: list[str] | None,
) -> int:
    """Atomically create/update receipts for notifications visible to a member.

    Eligibility is selected from tenant-scoped notification rows inside the same
    statement. Supplying another tenant's ID, or a notification targeted at a
    different member, therefore inserts nothing even under a forged request.
    """
    if field not in {"read_at", "dismissed_at"}:
        raise ValueError("unsupported notification receipt field")
    if ids == []:
        return 0

    table = await reflect_table("notifications")
    receipts = await reflect_table("notification_receipts")
    now = datetime.now(timezone.utc)
    existing = table.outerjoin(
        receipts,
        _receipt_join(table, receipts, organization_id, member_id),
    )
    eligible = select(
        literal(organization_id),
        table.c.region,
        table.c.id,
        literal(member_id),
        literal(now) if field == "read_at" else receipts.c.read_at,
        literal(now) if field == "dismissed_at" else receipts.c.dismissed_at,
        literal(now),
        literal(now),
    ).select_from(existing).where(
        _visible_to(table, organization_id, member_id),
        receipts.c[field].is_(None),
    )
    if ids is not None:
        eligible = eligible.where(table.c.id.in_(ids))

    statement = pg_insert(receipts).from_select(
        [
            "organization_id",
            "region",
            "notification_id",
            "member_id",
            "read_at",
            "dismissed_at",
            "created_at",
            "updated_at",
        ],
        eligible,
    ).on_conflict_do_update(
        index_elements=["organization_id", "notification_id", "member_id"],
        set_={field: now, "updated_at": now},
        where=receipts.c[field].is_(None),
    )
    async with engine.begin() as conn:
        result = await conn.execute(statement)
    return result.rowcount or 0


async def mark_read(
    organization_id: str, member_id: str, ids: list[str] | None = None
) -> int:
    """Mark visible notifications read for exactly one member."""
    return await _set_receipt_timestamp(organization_id, member_id, "read_at", ids)


async def dismiss(organization_id: str, member_id: str, ids: list[str]) -> int:
    """Dismiss visible notifications for exactly one member."""
    return await _set_receipt_timestamp(organization_id, member_id, "dismissed_at", ids)
