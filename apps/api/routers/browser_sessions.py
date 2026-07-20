from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from connectors.browser_operator import browser_operator
from core import permissions
from core.auth import get_current_member
from core.db import engine, reflect_table
from core.models import Member

router = APIRouter(prefix="/browser-sessions", tags=["browser-sessions"])
_SESSION_ADMIN_ROLES = {"admin", "owner"}


def _visible_member_id(member: Member) -> str | None:
    return None if member.role in _SESSION_ADMIN_ROLES else str(member.id)


async def _require_visible_session(session_id: str, member: Member) -> dict[str, Any]:
    sessions = await browser_operator.list_sessions(
        organization_id=member.organization_id,
        member_id=_visible_member_id(member),
    )
    for session in sessions:
        if str(session["id"]) == session_id:
            return session
    raise HTTPException(status_code=404, detail="Browser session not found")


class CreateBrowserSessionRequest(BaseModel):
    task_id: str | None = None
    consent: dict[str, Any] = Field(default_factory=dict)

    @field_validator("consent")
    @classmethod
    def validate_manual_consent(cls, consent: dict[str, Any]) -> dict[str, Any]:
        purpose = str(consent.get("purpose") or "").strip()
        domains = [str(value).strip().lower() for value in consent.get("allowed_domains") or []]
        expires_raw = str(consent.get("expires_at") or "")
        if len(purpose) < 3 or len(purpose) > 500:
            raise ValueError("browser consent purpose must be 3 to 500 characters")
        if not domains or len(domains) > 20:
            raise ValueError("browser consent must allow 1 to 20 domains")
        domain_pattern = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
        if any(not domain_pattern.fullmatch(domain) for domain in domains):
            raise ValueError("browser consent contains an invalid domain")
        try:
            expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("browser consent expiry is required") from exc
        now = datetime.now(timezone.utc)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now or expires_at > now + timedelta(hours=4):
            raise ValueError("browser consent expiry must be within the next 4 hours")
        if consent.get("confirmed_by_user") is not True:
            raise ValueError("browser consent must be explicitly confirmed")
        return {
            **consent,
            "purpose": purpose,
            "allowed_domains": sorted(set(domains)),
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
            "confirmed_by_user": True,
        }


class HandBackRequest(BaseModel):
    summary: str


class RequestTakeoverRequest(BaseModel):
    reason: str = "User requested manual control"


class ApproveSensitiveSiteRequest(BaseModel):
    domain: str
    approval_id: str | None = None


class RevokeBrowserSessionRequest(BaseModel):
    reason: str = "revoked by user"


@router.get("/")
async def list_browser_sessions(
    task_id: str | None = Query(default=None),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_browser_sessions", task_id or member.organization_id)
    return await browser_operator.list_sessions(
        organization_id=member.organization_id,
        task_id=task_id,
        member_id=_visible_member_id(member),
    )


@router.post("/")
async def create_browser_session(
    req: CreateBrowserSessionRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "create_browser_session", req.task_id or member.organization_id)
    return await browser_operator.create_session(
        organization_id=member.organization_id,
        member_id=member.id,
        task_id=req.task_id,
        consent=req.consent,
    )


@router.get("/{session_id}")
async def get_browser_session(session_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "view_browser_session", session_id)
    return await _require_visible_session(session_id, member)


@router.get("/{session_id}/events")
async def list_browser_session_events(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "view_browser_session_events", session_id)
    await _require_visible_session(session_id, member)
    try:
        events = await reflect_table("browser_session_events")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(events)
                    .where(events.c.organization_id == member.organization_id, events.c.session_id == session_id)
                    .order_by(events.c.seq.asc())
                    .limit(limit)
                )
            ).mappings().all()
        return [dict(row) for row in rows]
    except Exception:
        session = await get_browser_session(session_id, member)
        return session.get("history") or []


@router.get("/{session_id}/screenshot")
async def get_browser_session_screenshot(
    session_id: str,
    member: Member = Depends(get_current_member),
) -> Response:
    await permissions.check(member, "view_browser_session", session_id)
    await _require_visible_session(session_id, member)
    try:
        body, content_type = await browser_operator.screenshot_object(
            session_id,
            organization_id=member.organization_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Browser screenshot not found") from exc
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/{session_id}/downloads/{download_index}")
async def get_browser_session_download(
    session_id: str,
    download_index: int,
    member: Member = Depends(get_current_member),
) -> Response:
    await permissions.check(member, "view_browser_session", session_id)
    await _require_visible_session(session_id, member)
    try:
        body, record = await browser_operator.download_object(
            session_id,
            download_index,
            organization_id=member.organization_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Browser download not found") from exc
    filename = quote(str(record.get("filename") or "download.bin"), safe="")
    return Response(
        content=body,
        media_type=str(record.get("content_type") or "application/octet-stream"),
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{session_id}/live-view")
async def get_browser_session_live_view(
    session_id: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "view_browser_session", session_id)
    session = await _require_visible_session(session_id, member)
    if session.get("takeover_state") != "requested":
        raise HTTPException(status_code=409, detail="Request takeover before opening live view")
    return await browser_operator.live_view(
        session_id,
        organization_id=member.organization_id,
    )


@router.post("/{session_id}/hand-back")
async def hand_back_browser_session(
    session_id: str,
    req: HandBackRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "hand_back_browser_session", session_id)
    await _require_visible_session(session_id, member)
    return await browser_operator.hand_back(
        session_id,
        organization_id=member.organization_id,
        member_id=member.id,
        summary=req.summary,
    )


@router.post("/{session_id}/request-takeover")
async def request_browser_takeover(
    session_id: str,
    req: RequestTakeoverRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "request_browser_takeover", session_id)
    await _require_visible_session(session_id, member)
    return await browser_operator.request_takeover(
        session_id,
        organization_id=member.organization_id,
        reason=req.reason,
    )


@router.post("/{session_id}/approve-sensitive-site")
async def approve_sensitive_site(
    session_id: str,
    req: ApproveSensitiveSiteRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "approve_browser_sensitive_site", session_id)
    await _require_visible_session(session_id, member)
    return await browser_operator.approve_sensitive_site(
        session_id,
        organization_id=member.organization_id,
        member_id=member.id,
        domain=req.domain,
        approval_id=req.approval_id,
    )


@router.post("/{session_id}/revoke")
async def revoke_browser_session(
    session_id: str,
    req: RevokeBrowserSessionRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "revoke_browser_session", session_id)
    await _require_visible_session(session_id, member)
    return await browser_operator.revoke_session(
        session_id,
        organization_id=member.organization_id,
        member_id=member.id,
        reason=req.reason,
    )


@router.post("/{session_id}/close")
async def close_browser_session(session_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "close_browser_session", session_id)
    await _require_visible_session(session_id, member)
    return await browser_operator.close_session(session_id, organization_id=member.organization_id)
