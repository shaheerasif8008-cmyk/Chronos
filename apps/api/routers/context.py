from __future__ import annotations
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update

from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.context import ROOT
from core.db import engine, reflect_table
from core.models import Member
from jobs.context_update import propose_context_update

router = APIRouter(prefix="/context", tags=["context"])


class ContextSuggestionDecision(BaseModel):
    decision_note: str | None = None


def apply_context_patch(org_path: Path, suggested_patch: str) -> str:
    org_path.parent.mkdir(parents=True, exist_ok=True)
    current = org_path.read_text() if org_path.exists() else ""
    normalized_current = current.rstrip()
    normalized_patch = suggested_patch.strip()
    if normalized_current:
        next_content = f"{normalized_current}\n\n{normalized_patch}\n"
    else:
        next_content = f"{normalized_patch}\n"
    org_path.write_text(next_content)
    return next_content


@router.get("/suggestions")
async def list_context_suggestions(
    status: str = Query(default="pending"),
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await permissions.check(member, "list_context_suggestions", settings.org_id)
    context_suggestions = await reflect_table("context_suggestions")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(context_suggestions)
                .where(
                    context_suggestions.c.organization_id == member.organization_id,
                    context_suggestions.c.status == status,
                )
                .order_by(context_suggestions.c.created_at.desc())
            )
        ).mappings().all()
    return [dict(row) for row in rows]


@router.post("/suggestions/generate")
async def generate_context_suggestion(member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "generate_context_suggestion", settings.org_id)
    suggestion_id = await propose_context_update(member.organization_id)
    return {"id": suggestion_id, "created": suggestion_id is not None}


@router.post("/suggestions/{suggestion_id}/apply")
async def apply_context_suggestion(
    suggestion_id: str,
    req: ContextSuggestionDecision,
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "apply_context_suggestion", suggestion_id)
    context_suggestions = await reflect_table("context_suggestions")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(context_suggestions)
                .where(
                    context_suggestions.c.id == suggestion_id,
                    context_suggestions.c.organization_id == member.organization_id,
                    context_suggestions.c.status == "pending",
                )
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Pending context suggestion not found")

        org_path = Path(ROOT) / "context" / member.organization_id / "org.md"
        apply_context_patch(org_path, row["suggested_patch"])
        await conn.execute(
            update(context_suggestions)
            .where(context_suggestions.c.id == suggestion_id)
            .values(status="applied")
        )

    await audit.log(
        "context_update_applied",
        member.id,
        "context.apply_suggestion",
        resource_type="context_suggestions",
        resource_id=suggestion_id,
        payload={"decision_note": req.decision_note},
    )
    return {"id": suggestion_id, "status": "applied"}


@router.post("/suggestions/{suggestion_id}/reject")
async def reject_context_suggestion(
    suggestion_id: str,
    req: ContextSuggestionDecision,
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "reject_context_suggestion", suggestion_id)
    context_suggestions = await reflect_table("context_suggestions")
    async with engine.begin() as conn:
        result = await conn.execute(
            update(context_suggestions)
            .where(
                context_suggestions.c.id == suggestion_id,
                context_suggestions.c.organization_id == member.organization_id,
                context_suggestions.c.status == "pending",
            )
            .values(status="rejected")
            .returning(context_suggestions.c.id)
        )
        rejected = result.scalar_one_or_none()
    if rejected is None:
        raise HTTPException(status_code=404, detail="Pending context suggestion not found")

    await audit.log(
        "context_update_rejected",
        member.id,
        "context.reject_suggestion",
        resource_type="context_suggestions",
        resource_id=suggestion_id,
        payload={"decision_note": req.decision_note},
    )
    return {"id": suggestion_id, "status": "rejected"}
