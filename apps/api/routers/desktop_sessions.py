from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from connectors.desktop import desktop_connector
from core import permissions
from core.auth import get_current_member
from core.models import AgentContext, Member
from core.tool_broker import tool_broker

router = APIRouter(prefix="/desktop-sessions", tags=["desktop-sessions"])
_SESSION_ADMIN_ROLES = {"admin", "owner"}


def _visible_member_id(member: Member) -> str | None:
    return None if member.role in _SESSION_ADMIN_ROLES else str(member.id)


async def _require_visible_session(session_id: str, member: Member) -> dict[str, Any]:
    sessions = await desktop_connector.list_sessions(
        organization_id=member.organization_id,
        member_id=_visible_member_id(member),
    )
    for session in sessions:
        if str(session["id"]) == session_id:
            return session
    raise HTTPException(status_code=404, detail="Desktop session not found")


class CreateDesktopSessionRequest(BaseModel):
    task_id: str | None = None
    purpose: str = "desktop task"
    consent: dict[str, Any] = Field(default_factory=dict)

    @field_validator("consent")
    @classmethod
    def validate_manual_consent(cls, consent: dict[str, Any]) -> dict[str, Any]:
        purpose = str(consent.get("purpose") or "").strip()
        resources = [str(value).strip() for value in consent.get("allowed_resources") or []]
        if len(purpose) < 3 or len(purpose) > 500:
            raise ValueError("desktop consent purpose must be 3 to 500 characters")
        if not resources or len(resources) > 20 or any(len(value) > 160 for value in resources):
            raise ValueError("desktop consent must name 1 to 20 allowed resources")
        try:
            expires_at = datetime.fromisoformat(str(consent.get("expires_at") or "").replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("desktop consent expiry is required") from exc
        now = datetime.now(timezone.utc)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now or expires_at > now + timedelta(hours=4):
            raise ValueError("desktop consent expiry must be within the next 4 hours")
        if consent.get("confirmed_by_user") is not True:
            raise ValueError("desktop consent must be explicitly confirmed")
        return {
            **consent,
            "purpose": purpose,
            "allowed_resources": sorted(set(resources)),
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
            "confirmed_by_user": True,
        }


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
    return await desktop_connector.list_sessions(
        organization_id=member.organization_id,
        task_id=task_id,
        member_id=_visible_member_id(member),
    )


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
    if "session" not in result.data:
        raise HTTPException(
            status_code=503,
            detail=result.data.get("reason") or "Desktop runtime is unavailable",
        )
    return result.data["session"]


@router.get("/{session_id}/events")
async def list_desktop_session_events(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "view_desktop_session_events", session_id)
    await _require_visible_session(session_id, member)
    events = await desktop_connector.list_events(session_id, organization_id=member.organization_id)
    if not events:
        sessions = await desktop_connector.list_sessions(
            organization_id=member.organization_id,
            member_id=_visible_member_id(member),
        )
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
    await _require_visible_session(session_id, member)
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
    await _require_visible_session(session_id, member)
    return await desktop_connector.revoke_session(
        session_id, organization_id=member.organization_id, reason=req.reason
    )


@router.post("/{session_id}/close")
async def close_desktop_session(
    session_id: str, member: Member = Depends(get_current_member)
) -> dict[str, Any]:
    await permissions.check(member, "close_desktop_session", session_id)
    await _require_visible_session(session_id, member)
    return await desktop_connector.close_session(session_id, organization_id=member.organization_id)
