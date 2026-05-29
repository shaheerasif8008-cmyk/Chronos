from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update

from core import permissions
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from runtime import task_runner
from runtime.executor import emit_activity

router = APIRouter(prefix="/approvals", tags=["approvals"])


class ApprovalDecision(BaseModel):
    decision: str
    note: str | None = None
    batch: bool = False


@router.get("/")
async def list_approvals(
    status: str = Query(default="pending"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await permissions.check(member, "list_approvals", settings.org_id)
    approvals = await reflect_table("approvals")
    stmt = (
        select(approvals)
        .where(approvals.c.organization_id == member.organization_id, approvals.c.status == status)
        .order_by(approvals.c.requested_at.asc())
        .limit(limit)
        .offset(offset)
    )
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(row) for row in rows]


@router.get("/{approval_id}")
async def get_approval(approval_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "view_approval", approval_id)
    approvals = await reflect_table("approvals")
    async with engine.begin() as conn:
        row = (await conn.execute(select(approvals).where(approvals.c.id == approval_id))).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Approval not found")
    return dict(row)


@router.post("/{approval_id}/decide")
async def decide_approval(
    approval_id: str,
    req: ApprovalDecision,
    member: Member = Depends(get_current_member),
) -> dict:
    if req.decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="decision must be approved or rejected")
    await permissions.check(member, "decide_approval", approval_id)
    approvals = await reflect_table("approvals")
    decided_at = datetime.now(timezone.utc)

    async with engine.begin() as conn:
        row = (await conn.execute(select(approvals).where(approvals.c.id == approval_id))).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Approval not found")
        row_dict = dict(row)
        batch_id = (row_dict.get("action_payload") or {}).get("batch_id")
        stmt = update(approvals).where(approvals.c.id == approval_id)
        if req.batch and batch_id:
            stmt = update(approvals).where(
                approvals.c.task_id == row_dict["task_id"],
                approvals.c.step_id == row_dict["step_id"],
                approvals.c.status == "pending",
            )
        result = await conn.execute(
            stmt.values(
                status=req.decision,
                decided_by=member.id,
                decided_at=decided_at,
                decision_note=req.note,
            )
        )

    await emit_activity(
        row_dict["task_id"],
        {
            "type": "approval_decided",
            "approval_id": approval_id,
            "decision": req.decision,
            "batch": req.batch,
            "updated_count": result.rowcount,
        },
        actor_id=member.id,
    )
    await task_runner.enqueue_task(row_dict["task_id"])
    return {"status": "accepted", "approval_id": approval_id, "decision": req.decision, "resuming": True}
