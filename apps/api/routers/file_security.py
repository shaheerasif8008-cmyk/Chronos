"""Admin-only file quarantine review.

The queue exposes metadata-only verdict evidence. Infected/rejected bytes were
discarded at ingress and cannot be restored by a review decision; marking a
false positive records operator judgment for tuning and support follow-up only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, update

from core import audit, permissions
from core.auth import get_current_member
from core.db import engine, reflect_table
from core.models import Member
from core.settings_store import require_admin


router = APIRouter(prefix="/admin/file-quarantine", tags=["file-security"])


class ReviewRequest(BaseModel):
    status: Literal["acknowledged", "false_positive", "closed"]
    note: str = Field(default="", max_length=1000)
    confirmation: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_false_positive(self) -> "ReviewRequest":
        if self.status == "false_positive":
            if len(self.note.strip()) < 10:
                raise ValueError("false-positive reviews require a meaningful note")
            if self.confirmation != "MARK FALSE POSITIVE":
                raise ValueError("type MARK FALSE POSITIVE to confirm")
        return self


def _public_event(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in (
            "id",
            "source",
            "source_ref",
            "filename",
            "mime_type",
            "size_bytes",
            "sha256",
            "verdict",
            "engine",
            "engine_version",
            "signature",
            "error_code",
            "content_disarm_status",
            "content_disarm_reason",
            "review_status",
            "review_note",
            "reviewed_by",
            "reviewed_at",
            "scanned_at",
        )
    }


@router.get("")
async def list_quarantine_events(
    review_status: Literal["pending", "acknowledged", "false_positive", "closed", "all"] = Query(default="pending"),
    verdict: Literal["clean", "infected", "error", "all"] = Query(default="all"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
    member: Member = Depends(get_current_member),
) -> dict:
    require_admin(member)
    await permissions.check(member, "manage_file_quarantine", member.organization_id)
    events = await reflect_table("file_security_events")
    filters = [events.c.organization_id == member.organization_id]
    if review_status != "all":
        filters.append(events.c.review_status == review_status)
    if verdict != "all":
        filters.append(events.c.verdict == verdict)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(events)
                .where(*filters)
                .order_by(events.c.scanned_at.desc(), events.c.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).mappings().all()
        total = int(
            (
                await conn.execute(
                    select(func.count()).select_from(events).where(*filters)
                )
            ).scalar_one()
        )
        pending = int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(events)
                    .where(
                        events.c.organization_id == member.organization_id,
                        events.c.review_status == "pending",
                    )
                )
            ).scalar_one()
        )
    await audit.log(
        "file_quarantine_viewed",
        member.id,
        "manage_file_quarantine",
        organization_id=member.organization_id,
        resource_type="file_security_events",
        resource_id=member.organization_id,
        payload={"review_status": review_status, "verdict": verdict, "result_count": len(rows)},
    )
    return {
        "items": [_public_event(dict(row)) for row in rows],
        "total": total,
        "pending": pending,
        "limit": limit,
        "offset": offset,
    }


@router.patch("/{event_id}")
async def review_quarantine_event(
    event_id: str,
    request: ReviewRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    require_admin(member)
    await permissions.check(member, "manage_file_quarantine", event_id)
    events = await reflect_table("file_security_events")
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        current = (
            await conn.execute(
                select(events)
                .where(
                    events.c.id == event_id,
                    events.c.organization_id == member.organization_id,
                )
                .with_for_update()
            )
        ).mappings().first()
        if current is None:
            raise HTTPException(status_code=404, detail="Quarantine event not found")
        updated = (
            await conn.execute(
                update(events)
                .where(
                    events.c.id == event_id,
                    events.c.organization_id == member.organization_id,
                )
                .values(
                    review_status=request.status,
                    review_note=request.note.strip() or None,
                    reviewed_by=member.id,
                    reviewed_at=now,
                )
                .returning(events)
            )
        ).mappings().one()
    await audit.log(
        "file_quarantine_reviewed",
        member.id,
        "manage_file_quarantine",
        organization_id=member.organization_id,
        resource_type="file_security_events",
        resource_id=event_id,
        payload={
            "from_status": str(current.get("review_status") or "pending"),
            "to_status": request.status,
            "note_present": bool(request.note.strip()),
            "bytes_restored": False,
        },
    )
    return _public_event(dict(updated))
