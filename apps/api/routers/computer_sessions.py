from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from connectors.computer import computer_connector
from core import permissions
from core.auth import get_current_member
from core.models import AgentContext, Member
from core.tool_broker import tool_broker

router = APIRouter(prefix="/computer-sessions", tags=["computer-sessions"])
_SESSION_ADMIN_ROLES = {"admin", "owner"}


def _visible_member_id(member: Member) -> str | None:
    return None if member.role in _SESSION_ADMIN_ROLES else str(member.id)


async def _require_visible_session(session_id: str, member: Member) -> dict[str, Any]:
    sessions = await computer_connector.list_sessions(
        organization_id=member.organization_id,
        member_id=_visible_member_id(member),
    )
    for session in sessions:
        if str(session["id"]) == session_id:
            return session
    raise HTTPException(status_code=404, detail="Computer session not found")


async def _require_visible_grant(grant_id: str, member: Member) -> dict[str, Any]:
    grants = await computer_connector.list_local_grants(
        organization_id=member.organization_id,
        member_id=_visible_member_id(member),
    )
    for grant in grants:
        if str(grant["id"]) == grant_id:
            return grant
    raise HTTPException(status_code=404, detail="Local computer grant not found")


class ComputerConsent(BaseModel):
    purpose: str = Field(min_length=3, max_length=500)
    capabilities: list[
        Literal["terminal", "files", "packages", "desktop", "network"]
    ] = Field(min_length=1, max_length=5)
    expires_at: datetime
    confirmed_by_user: bool

    @model_validator(mode="after")
    def validate_window(self) -> "ComputerConsent":
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        if expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ValueError("expires_at must be in the future")
        if self.confirmed_by_user is not True:
            raise ValueError("confirmed_by_user must be true")
        if "packages" in self.capabilities and "network" not in self.capabilities:
            raise ValueError("packages requires the network capability")
        return self


class CreateComputerSessionRequest(BaseModel):
    task_id: str | None = None
    purpose: str = Field(min_length=3, max_length=500)
    consent: ComputerConsent

    @model_validator(mode="after")
    def consent_matches_purpose(self) -> "CreateComputerSessionRequest":
        if self.consent.purpose.strip() != self.purpose.strip():
            raise ValueError("consent purpose must match session purpose")
        return self


class ComputerInputRequest(BaseModel):
    action: Literal["move", "click", "double_click", "type", "key", "scroll", "drag"]
    x: int | None = None
    y: int | None = None
    to_x: int | None = None
    to_y: int | None = None
    button: Literal["left", "middle", "right"] = "left"
    text: str | None = Field(default=None, max_length=4000)
    key: str | None = Field(default=None, max_length=80)
    direction: Literal["up", "down"] = "down"
    amount: int = Field(default=1, ge=1, le=20)


class CreateLocalGrantRequest(BaseModel):
    folder_path: str
    purpose: str = "local computer task"
    task_id: str | None = None


class RevokeLocalGrantRequest(BaseModel):
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
async def list_computer_sessions(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_computer_sessions", member.organization_id)
    return await computer_connector.list_sessions(
        organization_id=member.organization_id,
        member_id=_visible_member_id(member),
    )


@router.post("/")
async def create_computer_session(
    req: CreateComputerSessionRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "create_computer_session", req.task_id or member.organization_id)
    result = await tool_broker.execute(
        _agent(member, req.task_id),
        "computer.create_session",
        {
            "purpose": req.purpose.strip(),
            "consent": req.consent.model_dump(mode="json"),
            "__approved_by_gate": True,
        },
    )
    if "session" not in result.data:
        raise HTTPException(
            status_code=503,
            detail=result.data.get("reason") or "Cloud computer runtime is unavailable",
        )
    return result.data["session"]


@router.get("/{session_id}/screenshot")
async def computer_screenshot(
    session_id: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "view_computer_session_events", session_id)
    await _require_visible_session(session_id, member)
    result = await tool_broker.execute(
        _agent(member),
        "computer.screenshot",
        {"session_id": session_id},
    )
    return result.data


@router.post("/{session_id}/input")
async def computer_input(
    session_id: str,
    req: ComputerInputRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "create_computer_session", session_id)
    await _require_visible_session(session_id, member)
    result = await tool_broker.execute(
        _agent(member),
        "computer.input",
        {
            "session_id": session_id,
            **req.model_dump(exclude_none=True),
            "__approved_by_gate": True,
        },
    )
    return result.data


@router.post("/{session_id}/pause")
async def pause_computer_session(
    session_id: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "create_computer_session", session_id)
    await _require_visible_session(session_id, member)
    result = await tool_broker.execute(
        _agent(member),
        "computer.pause_session",
        {"session_id": session_id},
    )
    return result.data["session"]


@router.post("/{session_id}/resume")
async def resume_computer_session(
    session_id: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "create_computer_session", session_id)
    await _require_visible_session(session_id, member)
    result = await tool_broker.execute(
        _agent(member),
        "computer.resume_session",
        {"session_id": session_id},
    )
    return result.data["session"]


@router.post("/{session_id}/cancel")
async def cancel_computer_session(
    session_id: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "create_computer_session", session_id)
    await _require_visible_session(session_id, member)
    result = await tool_broker.execute(
        _agent(member),
        "computer.cancel_session",
        {"session_id": session_id, "__approved_by_gate": True},
    )
    return result.data["session"]


@router.get("/{session_id}/events")
async def list_computer_events(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "view_computer_session_events", session_id)
    await _require_visible_session(session_id, member)
    events = await computer_connector.list_events(session_id, organization_id=member.organization_id)
    if not events:
        sessions = await computer_connector.list_sessions(
            organization_id=member.organization_id,
            member_id=_visible_member_id(member),
        )
        if not any(str(session["id"]) == session_id for session in sessions):
            raise HTTPException(status_code=404, detail="Computer session not found")
    return events[:limit]


@router.get("/local-grants")
async def list_local_grants(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_local_computer_grants", member.organization_id)
    return await computer_connector.list_local_grants(
        organization_id=member.organization_id,
        member_id=_visible_member_id(member),
    )


@router.post("/local-grants")
async def create_local_grant(
    req: CreateLocalGrantRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "create_local_computer_grant", req.task_id or member.organization_id)
    result = await tool_broker.execute(
        _agent(member, req.task_id),
        "local_computer.grant",
        {"folder_path": req.folder_path, "purpose": req.purpose, "member_id": member.id},
    )
    if "grant" not in result.data:
        raise HTTPException(
            status_code=503,
            detail=result.data.get("reason") or "Local computer runtime is unavailable",
        )
    return result.data["grant"]


@router.post("/local-grants/{grant_id}/revoke")
async def revoke_local_grant(
    grant_id: str,
    _req: RevokeLocalGrantRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "revoke_local_computer_grant", grant_id)
    await _require_visible_grant(grant_id, member)
    result = await tool_broker.execute(_agent(member), "local_computer.revoke", {"grant_id": grant_id})
    return result.data["grant"]


@router.get("/local-grants/{grant_id}/events")
async def list_local_grant_events(
    grant_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "view_local_computer_events", grant_id)
    await _require_visible_grant(grant_id, member)
    events = await computer_connector.list_local_events(grant_id, organization_id=member.organization_id)
    if not events:
        grants = await computer_connector.list_local_grants(
            organization_id=member.organization_id,
            member_id=_visible_member_id(member),
        )
        if not any(str(grant["id"]) == grant_id for grant in grants):
            raise HTTPException(status_code=404, detail="Local computer grant not found")
    return events[:limit]
