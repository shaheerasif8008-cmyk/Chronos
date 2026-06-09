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


def approval_summary(action_type: str, payload: dict | None) -> str:
    """Plain-English description of a pending action for the approval UI.

    Built from the action type plus the most recognizable payload fields so the
    approver never has to read raw tool arguments.
    """
    payload = payload or {}
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    merged = {**payload, **args}
    action = (action_type or "").replace("__", ".").lower()

    recipient = merged.get("to") or merged.get("recipient") or merged.get("email")
    subject = merged.get("subject") or merged.get("title")
    target = merged.get("url") or merged.get("target") or merged.get("path")

    if "gmail" in action or "email" in action or "mail" in action:
        verb = "Create an email draft" if "draft" in action else "Send an email"
        parts = [verb]
        if recipient:
            parts.append(f"to {recipient}")
        if subject:
            parts.append(f"— “{subject}”")
        return " ".join(parts)
    if "calendar" in action or "event" in action:
        base = "Create a calendar event" if ("create" in action or "add" in action) else "Update the calendar"
        return f"{base}{f' — “{subject}”' if subject else ''}"
    if "publish" in action or "post" in action:
        return f"Publish content{f' to {target}' if target else ''}{f' — “{subject}”' if subject else ''}"
    if "delete" in action or "remove" in action:
        return f"Delete {target or subject or 'records'}"

    # Generic fallback: humanize the action name, append the clearest target.
    readable = action.replace(".", " ").replace("_", " ").strip() or "run an action"
    suffix = f" — {recipient or subject or target}" if (recipient or subject or target) else ""
    return f"Run “{readable}”{suffix}"


def _with_summary(row: dict) -> dict:
    row["summary"] = approval_summary(
        str(row.get("action_type") or ""), row.get("action_payload")
    )
    return row


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
    return [_with_summary(dict(row)) for row in rows]


@router.get("/{approval_id}")
async def get_approval(approval_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "view_approval", approval_id)
    approvals = await reflect_table("approvals")
    async with engine.begin() as conn:
        row = (await conn.execute(select(approvals).where(approvals.c.id == approval_id))).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Approval not found")
    return _with_summary(dict(row))


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
