"""Attachment upload — stores raw bytes as an artifact. Parsing happens on first use."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Form
from sqlalchemy import insert

from core import audit, permissions
from core.artifacts import save_artifact
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member

router = APIRouter(prefix="/attachments", tags=["attachments"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


@router.post("")
async def upload_attachment(
    file: UploadFile = File(...),
    conversation_id: str | None = Form(default=None),
    project_id: str | None = Form(default=None),
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "upload_attachment", conversation_id or "new_conversation")
    if project_id:
        # Enforce membership before reading/storing (raises 404 for non-members).
        from routers.projects import _require_member
        await _require_member(member, project_id)
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 25 MB limit")

    attachment_id = await save_artifact(
        raw,
        kind="attachment",
        title=file.filename or "upload",
        conversation_id=conversation_id,
        mime_type=file.content_type,
        org_id=member.organization_id,
        parse_status="pending",
    )
    await audit.log(
        "attachment_uploaded", member.id, "attachments.upload",
        organization_id=member.organization_id,
        resource_type="artifacts", resource_id=attachment_id,
    )

    source_id = None
    if project_id:
        project_sources = await reflect_table("project_sources")
        async with engine.begin() as conn:
            src_result = await conn.execute(
                insert(project_sources)
                .values(
                    organization_id=member.organization_id,
                    region=settings.region,
                    project_id=project_id,
                    source_type="upload",
                    title=file.filename or "upload",
                    artifact_id=attachment_id,
                    parse_status="pending",
                    index_status="pending",
                    created_by=member.id,
                )
                .returning(project_sources.c.id)
            )
            source_id = str(src_result.scalar_one())
        await audit.log(
            "source_added", member.id, "attachments.add_source",
            organization_id=member.organization_id,
            resource_type="project_sources", resource_id=source_id,
            payload={"project_id": project_id},
        )
        # Fire-and-forget background index so upload returns immediately (still
        # index_status="pending"). index_source owns its error handling and marks
        # the source "failed" on error rather than crashing.
        from memory.source_indexing import index_source
        asyncio.create_task(index_source(source_id, member.organization_id))

    return {
        "attachment_id": attachment_id,
        "filename": file.filename,
        "mime_type": file.content_type,
        "size_bytes": len(raw),
        "source_id": source_id,
    }
