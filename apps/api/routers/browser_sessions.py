from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from connectors.browser_operator import browser_operator
from core import permissions
from core.auth import get_current_member
from core.db import engine, reflect_table
from core.models import Member

router = APIRouter(prefix="/browser-sessions", tags=["browser-sessions"])


class CreateBrowserSessionRequest(BaseModel):
    task_id: str | None = None
    consent: dict[str, Any] = {}


class HandBackRequest(BaseModel):
    summary: str


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
    return await browser_operator.list_sessions(organization_id=member.organization_id, task_id=task_id)


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
    sessions = await browser_operator.list_sessions(organization_id=member.organization_id)
    for session in sessions:
        if str(session["id"]) == session_id:
            return session
    raise HTTPException(status_code=404, detail="Browser session not found")


@router.get("/{session_id}/events")
async def list_browser_session_events(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "view_browser_session_events", session_id)
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


@router.post("/{session_id}/hand-back")
async def hand_back_browser_session(
    session_id: str,
    req: HandBackRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "hand_back_browser_session", session_id)
    return await browser_operator.hand_back(
        session_id,
        organization_id=member.organization_id,
        member_id=member.id,
        summary=req.summary,
    )


@router.post("/{session_id}/approve-sensitive-site")
async def approve_sensitive_site(
    session_id: str,
    req: ApproveSensitiveSiteRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "approve_browser_sensitive_site", session_id)
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
    return await browser_operator.revoke_session(
        session_id,
        organization_id=member.organization_id,
        member_id=member.id,
        reason=req.reason,
    )


@router.post("/{session_id}/close")
async def close_browser_session(session_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "close_browser_session", session_id)
    return await browser_operator.close_session(session_id, organization_id=member.organization_id)
