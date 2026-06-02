"""Artifacts router — workspace: list/get/content, versions, edit, AI-edit, restore, diff, rename, delete, publish."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select

from core import audit, permissions
from core.artifacts import (
    get_artifact,
    read_artifact_content,
    save_artifact,
    soft_delete_artifact,
    update_artifact_meta,
)
from core.artifact_shares import create_share, get_share_for_artifact, revoke_share
from core.artifact_versions import (
    create_version,
    diff_versions,
    list_versions,
    read_version_content,
    restore_version,
)
from core.auth import get_current_member
from core.db import engine, reflect_table
from core.llm import complete_text
from core.models import Member

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


async def _require(member: Member, action: str, artifact_id: str) -> dict:
    meta = await get_artifact(artifact_id)
    if not meta or str(meta.get("organization_id")) != str(member.organization_id) or meta.get("is_deleted"):
        raise HTTPException(status_code=404, detail="Artifact not found")
    if not await permissions.check(member, action, f"artifact:{artifact_id}"):
        raise HTTPException(status_code=403, detail="Not authorized")
    return meta


class EditBody(BaseModel):
    content: str
    mime_type: str | None = None
    edit_summary: str | None = None


class AIEditBody(BaseModel):
    instruction: str


class RenameBody(BaseModel):
    title: str


class CreateBody(BaseModel):
    content: str
    kind: str = "markdown"
    title: str | None = None
    mime_type: str | None = None
    conversation_id: str | None = None


@router.get("")
async def list_artifacts(
    conversation_id: str | None = None,
    task_id: str | None = None,
    kind: str | None = None,
    member: Member = Depends(get_current_member),
):
    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        q = select(artifacts).where(
            artifacts.c.organization_id == member.organization_id,
            artifacts.c.is_deleted == False,  # noqa: E712
        )
        if conversation_id:
            q = q.where(artifacts.c.conversation_id == conversation_id)
        if task_id:
            q = q.where(artifacts.c.task_id == task_id)
        if kind:
            q = q.where(artifacts.c.kind == kind)
        q = q.order_by(artifacts.c.created_at.desc()).limit(200)
        rows = (await conn.execute(q)).mappings().all()
    return [dict(r) for r in rows]


@router.post("")
async def create_artifact(body: CreateBody, member: Member = Depends(get_current_member)):
    if not await permissions.check(member, "artifact.create", "artifact:new"):
        raise HTTPException(status_code=403, detail="Not authorized")
    aid = await save_artifact(
        body.content,
        kind=body.kind,
        title=body.title,
        mime_type=body.mime_type,
        conversation_id=body.conversation_id,
        org_id=member.organization_id,
        created_by=f"member:{member.id}",
    )
    await audit.log("artifact", member.id, "artifact.create", organization_id=member.organization_id, resource_type="artifact", resource_id=aid)
    return await get_artifact(aid)


@router.get("/{artifact_id}")
async def get_artifact_metadata(artifact_id: str, member: Member = Depends(get_current_member)):
    return await _require(member, "artifact.read", artifact_id)


@router.get("/{artifact_id}/content")
async def download_artifact(artifact_id: str, member: Member = Depends(get_current_member)):
    meta = await _require(member, "artifact.read", artifact_id)
    content = await read_artifact_content(artifact_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Artifact content not found in storage")
    return Response(content=content, media_type=str(meta.get("mime_type") or "application/octet-stream"))


@router.get("/{artifact_id}/versions")
async def get_versions(artifact_id: str, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.read", artifact_id)
    return await list_versions(artifact_id, member.organization_id)


@router.get("/{artifact_id}/versions/{version}/content")
async def get_version_content(artifact_id: str, version: int, member: Member = Depends(get_current_member)):
    meta = await _require(member, "artifact.read", artifact_id)
    content = await read_version_content(artifact_id, version, member.organization_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return Response(content=content, media_type=str(meta.get("mime_type") or "application/octet-stream"))


@router.get("/{artifact_id}/diff")
async def get_diff(artifact_id: str, from_version: int, to_version: int, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.read", artifact_id)
    try:
        return await diff_versions(artifact_id, from_version, to_version, member.organization_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Version not found")


@router.post("/{artifact_id}/edit")
async def edit_artifact(artifact_id: str, body: EditBody, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.edit", artifact_id)
    updated = await create_version(
        artifact_id, body.content, org_id=member.organization_id,
        mime_type=body.mime_type, edit_summary=body.edit_summary or "manual edit",
        created_by=f"member:{member.id}",
    )
    await audit.log("artifact", member.id, "artifact.edit", organization_id=member.organization_id, resource_type="artifact",
                    resource_id=artifact_id, payload={"version": updated["version"]})
    return updated


@router.post("/{artifact_id}/ai-edit")
async def ai_edit_artifact(artifact_id: str, body: AIEditBody, member: Member = Depends(get_current_member)):
    meta = await _require(member, "artifact.edit", artifact_id)
    current = await read_artifact_content(artifact_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Artifact content not found")
    try:
        current_text = current.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="AI edit only supports text artifacts")
    prompt = (
        "You are editing a document artifact. Apply the user's instruction and return ONLY the "
        "full revised document content with no commentary, no code fences.\n\n"
        f"INSTRUCTION:\n{body.instruction}\n\nCURRENT CONTENT:\n{current_text}"
    )
    revised = await complete_text(prompt)
    updated = await create_version(
        artifact_id, revised, org_id=member.organization_id,
        mime_type=meta.get("mime_type"), edit_summary=f"AI edit: {body.instruction[:80]}",
        created_by=f"member:{member.id}",
    )
    await audit.log("artifact", member.id, "artifact.ai_edit", organization_id=member.organization_id, resource_type="artifact",
                    resource_id=artifact_id, payload={"version": updated["version"]})
    return updated


@router.post("/{artifact_id}/restore/{version}")
async def restore(artifact_id: str, version: int, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.edit", artifact_id)
    try:
        updated = await restore_version(artifact_id, version, org_id=member.organization_id,
                                        created_by=f"member:{member.id}")
    except ValueError:
        raise HTTPException(status_code=404, detail="Version not found")
    await audit.log("artifact", member.id, "artifact.restore", organization_id=member.organization_id, resource_type="artifact",
                    resource_id=artifact_id, payload={"restored_from": version, "version": updated["version"]})
    return updated


@router.patch("/{artifact_id}")
async def rename_artifact(artifact_id: str, body: RenameBody, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.edit", artifact_id)
    updated = await update_artifact_meta(artifact_id, member.organization_id, title=body.title)
    await audit.log("artifact", member.id, "artifact.rename", organization_id=member.organization_id, resource_type="artifact",
                    resource_id=artifact_id, payload={"title": body.title})
    return updated


@router.delete("/{artifact_id}")
async def delete_artifact(artifact_id: str, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.delete", artifact_id)
    await soft_delete_artifact(artifact_id, member.organization_id)
    await audit.log("artifact", member.id, "artifact.delete", organization_id=member.organization_id, resource_type="artifact", resource_id=artifact_id)
    return {"ok": True}


@router.post("/{artifact_id}/duplicate")
async def duplicate_artifact(artifact_id: str, member: Member = Depends(get_current_member)):
    """Copy an artifact's current content into a new, independent artifact (version 1)."""
    meta = await _require(member, "artifact.read", artifact_id)
    # Duplicating writes a brand-new artifact, so it requires create permission too.
    if not await permissions.check(member, "artifact.create", "artifact:new"):
        raise HTTPException(status_code=403, detail="Not authorized")
    content = await read_artifact_content(artifact_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Artifact content not found")
    base_title = meta.get("title") or "Untitled"
    new_id = await save_artifact(
        content,
        kind=str(meta.get("kind") or "file"),
        title=f"{base_title} (copy)",
        mime_type=meta.get("mime_type"),
        conversation_id=meta.get("conversation_id"),
        org_id=member.organization_id,
        created_by=f"member:{member.id}",
    )
    await audit.log("artifact", member.id, "artifact.duplicate", organization_id=member.organization_id, resource_type="artifact",
                    resource_id=new_id, payload={"source": artifact_id})
    return await get_artifact(new_id)


@router.post("/{artifact_id}/publish")
async def publish_artifact(artifact_id: str, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.publish", artifact_id)
    share = await create_share(artifact_id, org_id=member.organization_id, created_by=f"member:{member.id}")
    await audit.log("artifact", member.id, "artifact.publish", organization_id=member.organization_id, resource_type="artifact",
                    resource_id=artifact_id, decision="published")
    return {"token": share["token"], "status": share["status"], "share_path": f"/shared/{share['token']}"}


@router.post("/{artifact_id}/unpublish")
async def unpublish_artifact(artifact_id: str, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.publish", artifact_id)
    revoked = await revoke_share(artifact_id, member.organization_id)
    await audit.log("artifact", member.id, "artifact.unpublish", organization_id=member.organization_id, resource_type="artifact",
                    resource_id=artifact_id, decision="revoked")
    return {"revoked": revoked}


@router.get("/{artifact_id}/share")
async def share_status(artifact_id: str, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.read", artifact_id)
    share = await get_share_for_artifact(artifact_id, member.organization_id)
    if not share:
        return {"published": False}
    return {"published": True, "token": share["token"], "share_path": f"/shared/{share['token']}"}
