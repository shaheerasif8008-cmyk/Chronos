"""Public artifact share router — unauthenticated read by share token only."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from core import audit
from core.artifacts import get_artifact, read_artifact_content
from core.artifact_shares import get_active_share_by_token

router = APIRouter(prefix="/shared", tags=["artifact-share"])


@router.get("/{token}")
async def get_shared_metadata(token: str):
    share = await get_active_share_by_token(token)
    if not share:
        raise HTTPException(status_code=404, detail="Share not found or revoked")
    meta = await get_artifact(str(share["artifact_id"]))
    if not meta or meta.get("is_deleted"):
        raise HTTPException(status_code=404, detail="Artifact not found")
    await audit.log("artifact", None, "artifact.share_view", resource_type="artifact",
                    resource_id=str(share["artifact_id"]), payload={"token": token[:6] + "..."})
    return {"id": meta["id"], "title": meta.get("title"), "kind": meta.get("kind"),
            "mime_type": meta.get("mime_type"), "size_bytes": meta.get("size_bytes"),
            "version": meta.get("version")}


@router.get("/{token}/content")
async def get_shared_content(token: str):
    share = await get_active_share_by_token(token)
    if not share:
        raise HTTPException(status_code=404, detail="Share not found or revoked")
    meta = await get_artifact(str(share["artifact_id"]))
    if not meta or meta.get("is_deleted"):
        raise HTTPException(status_code=404, detail="Artifact not found")
    content = await read_artifact_content(str(share["artifact_id"]))
    if content is None:
        raise HTTPException(status_code=404, detail="Content not found")
    return Response(content=content, media_type=str(meta.get("mime_type") or "application/octet-stream"))
