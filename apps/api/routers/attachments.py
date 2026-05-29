"""Attachment upload — stores raw bytes as an artifact. Parsing happens on first use."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from core import audit, permissions
from core.artifacts import save_artifact
from core.auth import get_current_member
from core.models import Member

router = APIRouter(prefix="/attachments", tags=["attachments"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


@router.post("")
async def upload_attachment(
    file: UploadFile = File(...),
    conversation_id: str | None = Form(default=None),
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "upload_attachment", conversation_id or "new_conversation")
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
        resource_type="artifacts", resource_id=attachment_id,
    )
    return {
        "attachment_id": attachment_id,
        "filename": file.filename,
        "mime_type": file.content_type,
        "size_bytes": len(raw),
    }
