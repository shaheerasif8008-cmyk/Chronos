from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from connectors.computer import computer_connector
from core import permissions
from core.auth import get_current_member
from core.models import AgentContext, Member
from core.tool_broker import tool_broker

router = APIRouter(prefix="/computer-sessions", tags=["computer-sessions"])


class CreateComputerSessionRequest(BaseModel):
    task_id: str | None = None
    purpose: str = "computer task"


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
    return await computer_connector.list_sessions(organization_id=member.organization_id)


@router.post("/")
async def create_computer_session(
    req: CreateComputerSessionRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "create_computer_session", req.task_id or member.organization_id)
    result = await tool_broker.execute(
        _agent(member, req.task_id),
        "computer.create_session",
        {"purpose": req.purpose, "member_id": member.id},
    )
    return result.data["session"]


@router.get("/{session_id}/events")
async def list_computer_events(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "view_computer_session_events", session_id)
    events = await computer_connector.list_events(session_id, organization_id=member.organization_id)
    if not events:
        sessions = await computer_connector.list_sessions(organization_id=member.organization_id)
        if not any(str(session["id"]) == session_id for session in sessions):
            raise HTTPException(status_code=404, detail="Computer session not found")
    return events[:limit]


@router.get("/local-grants")
async def list_local_grants(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_local_computer_grants", member.organization_id)
    return await computer_connector.list_local_grants(organization_id=member.organization_id)


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
    return result.data["grant"]


@router.post("/local-grants/{grant_id}/revoke")
async def revoke_local_grant(
    grant_id: str,
    _req: RevokeLocalGrantRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "revoke_local_computer_grant", grant_id)
    result = await tool_broker.execute(_agent(member), "local_computer.revoke", {"grant_id": grant_id})
    return result.data["grant"]


@router.get("/local-grants/{grant_id}/events")
async def list_local_grant_events(
    grant_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "view_local_computer_events", grant_id)
    events = await computer_connector.list_local_events(grant_id, organization_id=member.organization_id)
    if not events:
        grants = await computer_connector.list_local_grants(organization_id=member.organization_id)
        if not any(str(grant["id"]) == grant_id for grant in grants):
            raise HTTPException(status_code=404, detail="Local computer grant not found")
    return events[:limit]
