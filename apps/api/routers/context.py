from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import insert, select, update
from sqlalchemy.sql import func

from core import audit, permissions
from core.auth import get_current_member
from core.db import engine, reflect_table
from core.models import Member
from jobs.context_update import (
    propose_context_update,
    validate_context_patch,
)

router = APIRouter(prefix="/context", tags=["context"])


class ContextSuggestionDecision(BaseModel):
    decision_note: str | None = None


@router.get("/suggestions")
async def list_context_suggestions(
    status: str = Query(default="pending"),
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await permissions.check(member, "list_context_suggestions", member.organization_id)
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
    await permissions.check(member, "generate_context_suggestion", member.organization_id)
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
    settings_documents = await reflect_table("settings_documents")
    unsafe_reason: str | None = None
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(context_suggestions)
                .where(
                    context_suggestions.c.id == suggestion_id,
                    context_suggestions.c.organization_id == member.organization_id,
                    context_suggestions.c.status == "pending",
                )
                .with_for_update()
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Pending context suggestion not found")

        try:
            approved_patch = validate_context_patch(row["suggested_patch"])
        except ValueError as exc:
            unsafe_reason = str(exc)
            await conn.execute(
                update(context_suggestions)
                .where(
                    context_suggestions.c.id == suggestion_id,
                    context_suggestions.c.organization_id == member.organization_id,
                )
                .values(status="rejected")
            )
        else:
            context_row = (
                await conn.execute(
                    select(settings_documents).where(
                        settings_documents.c.organization_id == member.organization_id,
                        settings_documents.c.scope == "org",
                        settings_documents.c.scope_id == member.organization_id,
                        settings_documents.c.section == "organization_context",
                    ).with_for_update()
                )
            ).mappings().first()
            values = dict(context_row["values"] or {}) if context_row else {}
            current = str(values.get("approved_markdown") or "").rstrip()
            if approved_patch not in current:
                values["approved_markdown"] = (
                    f"{current}\n\n{approved_patch}" if current else approved_patch
                )
            if context_row:
                await conn.execute(
                    update(settings_documents)
                    .where(settings_documents.c.id == context_row["id"])
                    .values(values=values, updated_by=member.id, updated_at=func.now())
                )
            else:
                await conn.execute(
                    insert(settings_documents).values(
                        organization_id=member.organization_id,
                        region=member.region,
                        scope="org",
                        scope_id=member.organization_id,
                        section="organization_context",
                        values=values,
                        updated_by=member.id,
                    )
                )
            await conn.execute(
                update(context_suggestions)
                .where(
                    context_suggestions.c.id == suggestion_id,
                    context_suggestions.c.organization_id == member.organization_id,
                )
                .values(status="applied")
            )

    if unsafe_reason is not None:
        await audit.log(
            "context_update_rejected",
            member.id,
            "context.apply_suggestion",
            organization_id=member.organization_id,
            resource_type="context_suggestions",
            resource_id=suggestion_id,
            payload={"reason": unsafe_reason[:240]},
            decision="unsafe_persisted_patch",
        )
        raise HTTPException(status_code=409, detail="Suggestion failed safety validation")

    await audit.log(
        "context_update_applied",
        member.id,
        "context.apply_suggestion",
        organization_id=member.organization_id,
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
        organization_id=member.organization_id,
        resource_type="context_suggestions",
        resource_id=suggestion_id,
        payload={"decision_note": req.decision_note},
    )
    return {"id": suggestion_id, "status": "rejected"}
