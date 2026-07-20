from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select, update

from core import notifications, permissions
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from runtime import task_runner
from runtime.executor import emit_activity

router = APIRouter(prefix="/approvals", tags=["approvals"])

_ORG_APPROVER_ROLES = {"admin", "owner", "approver"}


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
    tasks = await reflect_table("tasks")
    stmt = (
        select(approvals)
        .join(tasks, tasks.c.id == approvals.c.task_id)
        .where(approvals.c.organization_id == member.organization_id, approvals.c.status == status)
        .order_by(approvals.c.requested_at.asc())
        .limit(limit)
        .offset(offset)
    )
    if member.role not in _ORG_APPROVER_ROLES:
        stmt = stmt.where(
            tasks.c.organization_id == member.organization_id,
            or_(
                tasks.c.triggered_by_member_id == member.id,
                tasks.c.assignee_member_id == member.id,
            ),
        )
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [_with_summary(dict(row)) for row in rows]


@router.get("/{approval_id}")
async def get_approval(approval_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "view_approval", approval_id)
    approvals = await reflect_table("approvals")
    tasks = await reflect_table("tasks")
    conditions = [
        approvals.c.id == approval_id,
        approvals.c.organization_id == member.organization_id,
    ]
    if member.role not in _ORG_APPROVER_ROLES:
        conditions.extend(
            [
                tasks.c.organization_id == member.organization_id,
                or_(
                    tasks.c.triggered_by_member_id == member.id,
                    tasks.c.assignee_member_id == member.id,
                ),
            ]
        )
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(approvals).join(tasks, tasks.c.id == approvals.c.task_id).where(*conditions)
        )).mappings().first()
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
    delivery_receipts = await reflect_table("notification_delivery_receipts")
    agent_publications = await reflect_table("agent_publications")
    decided_at = datetime.now(timezone.utc)
    publication_reply = False

    async with engine.begin() as conn:
        row = (await conn.execute(
            select(approvals).where(
                approvals.c.id == approval_id,
                approvals.c.organization_id == member.organization_id,
            )
        )).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Approval not found")
        row_dict = dict(row)
        if row_dict["status"] != "pending":
            raise HTTPException(status_code=409, detail="Approval has already been decided")
        expires_at = row_dict.get("expires_at")
        if expires_at is not None and expires_at <= decided_at:
            raise HTTPException(status_code=409, detail="Approval has expired")
        publication_reply = row_dict.get("action_type") == "agent.publication.reply"
        batch_id = (row_dict.get("action_payload") or {}).get("batch_id")
        # Every mutation is scoped to the caller's org so a member can never
        # decide another tenant's approvals (RULE 9).
        stmt = update(approvals).where(
            approvals.c.id == approval_id,
            approvals.c.organization_id == member.organization_id,
        )
        if req.batch and batch_id:
            stmt = update(approvals).where(
                approvals.c.task_id == row_dict["task_id"],
                approvals.c.step_id == row_dict["step_id"],
                approvals.c.status == "pending",
                approvals.c.organization_id == member.organization_id,
            )
        result = await conn.execute(
            stmt.values(
                status=req.decision,
                decided_by=member.id,
                decided_at=decided_at,
                decision_note=req.note,
            )
        )
        if publication_reply:
            released_status = "pending"
            released_at = None
            error_code = None
            if req.decision == "rejected":
                released_status = "dead_letter"
                error_code = "approval_rejected"
            else:
                receipt_channel = (
                    await conn.execute(
                        select(delivery_receipts.c.channel).where(
                            delivery_receipts.c.organization_id == member.organization_id,
                            delivery_receipts.c.approval_id == approval_id,
                            delivery_receipts.c.status == "approval_pending",
                        )
                    )
                ).scalar_one_or_none()
                if receipt_channel in {"web", "api"}:
                    released_status = "delivered"
                    released_at = decided_at
            await conn.execute(
                update(delivery_receipts)
                .where(
                    delivery_receipts.c.organization_id == member.organization_id,
                    delivery_receipts.c.approval_id == approval_id,
                    delivery_receipts.c.status == "approval_pending",
                )
                .values(
                    status=released_status,
                    delivered_at=released_at,
                    last_error_code=error_code,
                    next_attempt_at=decided_at if released_status == "pending" else None,
                    updated_at=decided_at,
                )
            )
            if released_status == "delivered":
                publication_id = (
                    await conn.execute(
                        select(delivery_receipts.c.publication_id).where(
                            delivery_receipts.c.organization_id == member.organization_id,
                            delivery_receipts.c.approval_id == approval_id,
                        )
                    )
                ).scalar_one_or_none()
                if publication_id:
                    await conn.execute(
                        update(agent_publications)
                        .where(
                            agent_publications.c.id == publication_id,
                            agent_publications.c.organization_id == member.organization_id,
                        )
                        .values(
                            provider_status="ready",
                            last_outbound_at=decided_at,
                            last_error_code=None,
                            updated_at=decided_at,
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

    # Notify the org that an approval was decided (best-effort; gated by the
    # org's notification settings). Never let it break the decision response.
    try:
        await notifications.emit(
            organization_id=member.organization_id,
            type="approval_decision",
            title=f"Approval {req.decision}",
            body=approval_summary(
                str(row_dict.get("action_type") or ""), row_dict.get("action_payload")
            ),
            severity="success" if req.decision == "approved" else "warning",
            resource_type="task",
            resource_id=str(row_dict["task_id"]),
            created_by=str(member.id),
        )
    except Exception:
        pass

    # Feed the trust ledger and, on rejection, propose a learned guardrail from the
    # reviewer's note. Best-effort — never let it break the decision response.
    await _record_decision_to_trust(row_dict, req, member, approval_id)

    if not publication_reply:
        await task_runner.enqueue_task(row_dict["task_id"])
    return {
        "status": "accepted",
        "approval_id": approval_id,
        "decision": req.decision,
        "resuming": not publication_reply,
        **({"publication_response_released": req.decision == "approved"} if publication_reply else {}),
    }


async def _record_decision_to_trust(
    row_dict: dict, req: ApprovalDecision, member: Member, approval_id: str
) -> None:
    """Translate an approval decision into Graduated-Autonomy signal.

    approved -> positive trust event; rejected -> negative event (trips the
    circuit breaker for that action_class) plus a *proposed* learned policy
    synthesized from the reviewer's note.
    """
    try:
        from core import learned_policy, risk as risk_pricer, trust

        payload = row_dict.get("action_payload") or {}
        args = payload.get("args") if isinstance(payload.get("args"), dict) else payload
        tool = str(row_dict.get("action_type") or "").replace("__", ".")
        if not tool:
            return
        risk = risk_pricer.price(tool, args or {})

        # Resolve the workspace the action ran in so trust hits the right scope.
        workspace_id = None
        try:
            tasks = await reflect_table("tasks")
            async with engine.begin() as conn:
                task_row = (
                    await conn.execute(select(tasks.c.workspace_id).where(tasks.c.id == row_dict["task_id"]))
                ).first()
            workspace_id = task_row[0] if task_row else None
        except Exception:
            pass

        outcome = "approved" if req.decision == "approved" else "rejected"
        await trust.record_outcome(
            member.organization_id, workspace_id, risk, outcome,
            region=member.region, tool=tool, actor_id=member.id, approval_id=approval_id,
        )
        if req.decision == "rejected":
            await learned_policy.synthesize_from_rejection(
                org_id=member.organization_id, region=member.region,
                action_class=risk.action_class, args=args or {}, note=req.note,
                source_approval_id=approval_id,
            )
    except Exception:
        pass
