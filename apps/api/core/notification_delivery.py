"""Notification delivery channels (W5.3).

Drives outbound delivery (email) from the durable W5.2 notification records and
the per-org notification settings. Truthful-degraded: when no email provider is
configured the system records the attempt as ``degraded`` and does NOT mark the
notification as emailed (so nothing is silently dropped) — in-app notifications
keep working regardless.

The provider call is isolated in ``_provider_send_email`` so the rest is testable
without a live email account (tests monkeypatch it).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select, update

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.notifications import _notification_settings


class EmailNotConfigured(Exception):
    pass


def email_is_configured() -> bool:
    return bool(settings.sendgrid_api_key and settings.notification_from_email)


def _provider_send_email(*, to: str, subject: str, body: str) -> None:
    """Send one email. Real impl calls SendGrid; raises when the SDK/secret isn't
    available. Tests monkeypatch this."""
    raise EmailNotConfigured("Email provider (SendGrid) not wired in this build")


async def _org_admin_emails(organization_id: str) -> list[str]:
    members = await reflect_table("members")
    async with engine.begin() as conn:
        rows = (await conn.execute(
            select(members.c.email).where(
                members.c.organization_id == organization_id,
                members.c.role.in_(["owner", "admin"]),
            )
        )).all()
    return [r[0] for r in rows if r[0]]


async def deliver_pending(organization_id: str, *, limit: int = 100) -> dict[str, Any]:
    """Attempt email delivery for the org's not-yet-emailed notifications.

    Returns a truthful summary. When email is disabled for the org or no provider
    is configured, returns a ``skipped``/``degraded`` summary and leaves
    ``emailed_at`` untouched (the in-app record remains the source of truth).
    """
    notif_settings = await _notification_settings(organization_id)
    if notif_settings.get("email") is not True:
        return {"status": "skipped", "reason": "email_disabled_for_org", "delivered": 0}

    table = await reflect_table("notifications")
    async with engine.begin() as conn:
        rows = (await conn.execute(
            select(table).where(
                table.c.organization_id == organization_id,
                table.c.emailed_at.is_(None),
                table.c.dismissed_at.is_(None),
            ).order_by(table.c.created_at.asc()).limit(limit)
        )).mappings().all()
    pending = [dict(r) for r in rows]
    if not pending:
        return {"status": "ok", "delivered": 0, "pending": 0}

    if not email_is_configured():
        # Truthful-degraded: do not mark emailed; surface the unmet intent.
        await audit.log(
            "notification", "system", "notification_email_degraded",
            organization_id=organization_id, resource_type="notification",
            payload={"pending": len(pending), "reason": "provider_not_configured"},
        )
        return {"status": "degraded", "reason": "provider_not_configured",
                "delivered": 0, "pending": len(pending)}

    recipients = await _org_admin_emails(organization_id)
    delivered_ids: list[str] = []
    for n in pending:
        targets = recipients if n.get("member_id") is None else await _member_email(n["member_id"])
        try:
            for to in targets:
                _provider_send_email(to=to, subject=n["title"], body=n.get("body") or n["title"])
            delivered_ids.append(n["id"])
        except EmailNotConfigured:
            break
    if delivered_ids:
        now = datetime.now(timezone.utc)
        async with engine.begin() as conn:
            await conn.execute(update(table).where(and_(
                table.c.organization_id == organization_id,
                table.c.id.in_(delivered_ids),
            )).values(emailed_at=now))
        await audit.log(
            "notification", "system", "notification_email_sent",
            organization_id=organization_id, resource_type="notification",
            payload={"delivered": len(delivered_ids)},
        )
    return {"status": "ok", "delivered": len(delivered_ids), "pending": len(pending) - len(delivered_ids)}


async def _member_email(member_id: str) -> list[str]:
    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(members.c.email).where(members.c.id == member_id)
        )).first()
    return [row[0]] if row and row[0] else []


async def build_digest(organization_id: str, member_id: str) -> dict[str, Any]:
    """Build a weekly-digest summary of the member's unread notifications, grouped
    by type. Used by the digest rollup (honest-degraded delivery via
    ``deliver_pending``); also directly renderable in-app."""
    from core import notifications

    items = await notifications.list_for(
        organization_id, member_id, unread_only=True, limit=200, offset=0
    )
    by_type: dict[str, int] = {}
    for n in items:
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
    return {
        "organization_id": organization_id,
        "member_id": member_id,
        "unread_total": len(items),
        "by_type": by_type,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
