"""Artifacts router — read artifact metadata and download content."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select

from core.artifacts import get_artifact, read_artifact_content
from core.auth import get_current_member
from core.db import engine, reflect_table
from core.models import Member

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.get("")
async def list_artifacts(
    conversation_id: str | None = None,
    task_id: str | None = None,
    member: Member = Depends(get_current_member),
):
    """List artifacts for a conversation or task."""
    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        q = select(artifacts).where(artifacts.c.organization_id == member.organization_id)
        if conversation_id:
            q = q.where(artifacts.c.conversation_id == conversation_id)
        if task_id:
            q = q.where(artifacts.c.task_id == task_id)
        q = q.order_by(artifacts.c.created_at.desc()).limit(50)
        rows = (await conn.execute(q)).mappings().all()
    return [dict(row) for row in rows]


@router.get("/{artifact_id}")
async def get_artifact_metadata(
    artifact_id: str,
    member: Member = Depends(get_current_member),
):
    """Return artifact metadata."""
    meta = await get_artifact(artifact_id)
    if not meta or str(meta.get("organization_id")) != str(member.organization_id):
        raise HTTPException(status_code=404, detail="Artifact not found")
    return meta


@router.get("/{artifact_id}/content")
async def download_artifact(
    artifact_id: str,
    member: Member = Depends(get_current_member),
):
    """Stream artifact content bytes."""
    meta = await get_artifact(artifact_id)
    if not meta or str(meta.get("organization_id")) != str(member.organization_id):
        raise HTTPException(status_code=404, detail="Artifact not found")
    content = await read_artifact_content(artifact_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Artifact content not found in storage")
    mime = str(meta.get("mime_type") or "application/octet-stream")
    return Response(content=content, media_type=mime)
