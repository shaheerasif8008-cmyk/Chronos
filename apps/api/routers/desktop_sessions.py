from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from connectors.desktop import desktop_connector
from core import permissions
from core.auth import get_current_member
from core.models import AgentContext, Member
from core.tool_broker import tool_broker

router = APIRouter(prefix="/desktop-sessions", tags=["desktop-sessions"])


class CreateDesktopSessionRequest(BaseModel):
    task_id: str | None = None
    purpose: str = "desktop task"
    consent: dict[str, Any] = {}


class RevokeDesktopSessionRequest(BaseModel):
    reason: str = "revoked by user"


def _agent(member: Member, task_id: str | None = None) -> AgentContext:
    return AgentContext(
        id=f"member:{member.id}",
        org_id=member.organization_id,
        member_id=member.id,
        workspace_id="default",
        task_id=task_id,
    )


@router.get("/")
async def list_desktop_sessions(
    task_id: str | None = Query(default=None),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_desktop_sessions", task_id or member.organization_id)
    return await desktop_connector.list_sessions(organization_id=member.organization_id, task_id=task_id)


@router.post("/")
async def create_desktop_session(
    req: CreateDesktopSessionRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "create_desktop_session", req.task_id or member.organization_id)
    result = await tool_broker.execute(
        _agent(member, req.task_id),
        "desktop.create_session",
        {"purpose": req.purpose, "consent": req.consent, "member_id": member.id},
    )
    return result.data["session"]


@router.get("/{session_id}/events")
async def list_desktop_session_events(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "view_desktop_session_events", session_id)
    events = await desktop_connector.list_events(session_id, organization_id=member.organization_id)
    if not events:
        sessions = await desktop_connector.list_sessions(organization_id=member.organization_id)
        if not any(str(session["id"]) == session_id for session in sessions):
            raise HTTPException(status_code=404, detail="Desktop session not found")
    return events[:limit]


@router.get("/{session_id}/screenshot")
async def desktop_screenshot(
    session_id: str, member: Member = Depends(get_current_member)
) -> dict[str, Any]:
    """Capture and return a fresh desktop frame as a PNG data URL.

    Polling this drives a true pixel stream of the virtual desktop. When the
    runtime has no virtual display (headless), the connector returns a degraded
    result and ``screenshot_data_url`` is null — the caller shows a placeholder.
    """
    await permissions.check(member, "view_desktop_session_events", session_id)
    try:
        result = await tool_broker.execute(
            _agent(member), "desktop.screenshot", {"session_id": session_id}
        )
    except Exception as exc:  # never 500 a viewer poll
        return {"status": "unavailable", "screenshot_data_url": None, "reason": str(exc)[:200]}
    data = result.data or {}
    return {
        "status": data.get("status", "active"),
        "screenshot_data_url": data.get("screenshot_data_url"),
        "session": data.get("session"),
    }


@router.post("/{session_id}/revoke")
async def revoke_desktop_session(
    session_id: str,
    req: RevokeDesktopSessionRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "revoke_desktop_session", session_id)
    return await desktop_connector.revoke_session(
        session_id, organization_id=member.organization_id, reason=req.reason
    )


@router.post("/{session_id}/close")
async def close_desktop_session(
    session_id: str, member: Member = Depends(get_current_member)
) -> dict[str, Any]:
    await permissions.check(member, "close_desktop_session", session_id)
    return await desktop_connector.close_session(session_id, organization_id=member.organization_id)
