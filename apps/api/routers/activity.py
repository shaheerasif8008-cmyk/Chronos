from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from core import permissions
from core.activity_events import list_activity_actions
from core.auth import get_current_member
from core.models import Member

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("/actions")
async def get_activity_actions(
    type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    tool: str | None = Query(default=None),
    query: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await permissions.check(member, "list_activity_actions", member.organization_id)
    return await list_activity_actions(
        member.organization_id,
        event_type=type,
        status=status,
        task_id=task_id,
        tool=tool,
        query=query,
        limit=limit,
        offset=offset,
    )
