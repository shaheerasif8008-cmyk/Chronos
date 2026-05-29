"""Artifacts router — read artifact metadata, version history, and content."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select

from core import permissions
from core.artifacts import get_artifact, get_artifact_versions, read_artifact_content, save_artifact
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
    """List the current version of artifacts for a conversation or task."""
    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        q = select(artifacts).where(
            artifacts.c.organization_id == member.organization_id,
            artifacts.c.is_current.is_(True),
        )
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


@router.get("/{artifact_id}/versions")
async def list_artifact_versions(
    artifact_id: str,
    member: Member = Depends(get_current_member),
):
    """Return full artifact version history, oldest first."""
    meta = await get_artifact(artifact_id)
    if not meta or str(meta.get("organization_id")) != str(member.organization_id):
        raise HTTPException(status_code=404, detail="Artifact not found")
    return await get_artifact_versions(artifact_id)


class ArtifactEdit(BaseModel):
    content: str
    title: str | None = None


@router.post("/{artifact_id}/versions")
async def create_artifact_version(
    artifact_id: str,
    body: ArtifactEdit,
    member: Member = Depends(get_current_member),
):
    """Save a human edit as the next version in this artifact lineage."""
    meta = await get_artifact(artifact_id)
    if not meta or str(meta.get("organization_id")) != str(member.organization_id):
        raise HTTPException(status_code=404, detail="Artifact not found")
    await permissions.check(member, "artifact.edit", artifact_id)
    new_id = await save_artifact(
        body.content,
        kind=str(meta.get("kind") or "text"),
        title=body.title or meta.get("title"),
        key=str(meta["artifact_key"]),
        conversation_id=meta.get("conversation_id"),
        task_id=meta.get("task_id"),
        org_id=str(meta["organization_id"]),
        region=str(meta.get("region") or "us"),
        mime_type=meta.get("mime_type"),
    )
    return await get_artifact(new_id)


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
