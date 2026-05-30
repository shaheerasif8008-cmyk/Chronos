"""Public artifact share router — unauthenticated read by share token only."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from core import audit
from core.artifacts import get_artifact, read_artifact_content
from core.artifact_shares import get_active_share_by_token

router = APIRouter(prefix="/shared", tags=["artifact-share"])

# Active markup must never be served inline from this unauthenticated origin —
# it would execute script in the api origin, bypassing the renderer's sandbox.
_ACTIVE_MARKUP_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml", "image/svg"}


@router.get("/{token}")
async def get_shared_metadata(token: str):
    share = await get_active_share_by_token(token)
    if not share:
        raise HTTPException(status_code=404, detail="Share not found or revoked")
    meta = await get_artifact(str(share["artifact_id"]))
    if not meta or meta.get("is_deleted"):
        raise HTTPException(status_code=404, detail="Artifact not found")
    await audit.log("artifact", None, "artifact.share_view", resource_type="artifact",
                    resource_id=str(share["artifact_id"]), payload={"token": token[:6] + "..."},
                    organization_id=str(share["organization_id"]))
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
    mime = str(meta.get("mime_type") or "application/octet-stream")
    headers = {"X-Content-Type-Options": "nosniff"}
    # Force download + inert CSP for active markup so it can't execute in this origin.
    if mime.lower() in _ACTIVE_MARKUP_TYPES or mime.lower().endswith("+xml"):
        headers["Content-Disposition"] = "attachment"
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        mime = "application/octet-stream"
    return Response(content=content, media_type=mime, headers=headers)
