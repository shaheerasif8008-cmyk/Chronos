from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from core import notification_delivery, notifications, permissions
from core.auth import get_current_member
from core.models import Member
from core.settings_store import require_admin

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationIds(BaseModel):
    ids: list[str] | None = None


@router.get("")
async def list_notifications(
    include_dismissed: bool = False,
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    return await notifications.list_for(
        member.organization_id,
        str(member.id),
        include_dismissed=include_dismissed,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )


@router.get("/unread_count")
async def unread_count(member: Member = Depends(get_current_member)) -> dict[str, int]:
    return {"count": await notifications.unread_count(member.organization_id, str(member.id))}


@router.post("/read")
async def mark_read(
    body: NotificationIds | None = None,
    member: Member = Depends(get_current_member),
) -> dict[str, int]:
    ids = body.ids if body else None
    updated = await notifications.mark_read(member.organization_id, str(member.id), ids)
    return {"updated": updated}


@router.post("/dismiss")
async def dismiss(
    body: NotificationIds,
    member: Member = Depends(get_current_member),
) -> dict[str, int]:
    updated = await notifications.dismiss(member.organization_id, str(member.id), body.ids or [])
    return {"updated": updated}


@router.get("/digest")
async def digest(member: Member = Depends(get_current_member)) -> dict[str, Any]:
    """Weekly-digest summary of the member's unread notifications, grouped by type."""
    return await notification_delivery.build_digest(member.organization_id, str(member.id))


@router.post("/deliver")
async def deliver(member: Member = Depends(get_current_member)) -> dict[str, Any]:
    """Trigger outbound (email) delivery of pending notifications for the org.

    Admin-only. Truthful-degraded: returns a ``degraded`` summary when no email
    provider is configured rather than pretending to send.
    """
    require_admin(member)
    await permissions.check(member, "deliver_notifications", member.organization_id)
    return await notification_delivery.deliver_pending(member.organization_id)
