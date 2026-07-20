"""Public artifact share router — unauthenticated read by share token only."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from core import audit
from core.artifacts import get_artifact, read_artifact_content
from core.artifact_shares import get_active_share_by_token
from core.artifact_rendering import ArtifactPreviewError, build_preview, is_pdf_artifact, render_pdf_page, safe_download_headers
from core.config import settings

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
    await audit.log("artifact", None, "artifact.share_view",
                    organization_id=str(share["organization_id"]), resource_type="artifact",
                    resource_id=str(share["artifact_id"]), payload={"token": token[:6] + "..."})
    return {"id": meta["id"], "title": meta.get("title"), "kind": meta.get("kind"),
            "mime_type": meta.get("mime_type"), "size_bytes": meta.get("size_bytes"),
            "version": meta.get("version"), "expires_at": share.get("expires_at")}


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
    headers = safe_download_headers(meta.get("title"))
    headers["Cache-Control"] = "no-store"
    # Force download + inert CSP for active markup so it can't execute in this origin.
    if mime.lower() in _ACTIVE_MARKUP_TYPES or mime.lower().endswith("+xml"):
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        mime = "application/octet-stream"
    return Response(content=content, media_type=mime, headers=headers)


@router.get("/{token}/preview")
async def get_shared_preview(token: str):
    """Return the same inert preview contract used by authenticated views."""
    share = await get_active_share_by_token(token)
    if not share:
        raise HTTPException(status_code=404, detail="Share not found or revoked")
    meta = await get_artifact(str(share["artifact_id"]))
    if not meta or meta.get("is_deleted"):
        raise HTTPException(status_code=404, detail="Artifact not found")
    if int(meta.get("size_bytes") or 0) > settings.artifact_preview_max_bytes:
        return {
            "status": "unsupported",
            "renderer": "download",
            "format": "unknown",
            "mime_type": str(meta.get("mime_type") or "application/octet-stream"),
            "size_bytes": int(meta.get("size_bytes") or 0),
            "limitations": [
                f"Inline preview is limited to {settings.artifact_preview_max_bytes:,} bytes; download remains available."
            ],
        }
    content = await read_artifact_content(str(share["artifact_id"]))
    if content is None:
        raise HTTPException(status_code=404, detail="Content not found")
    return await asyncio.to_thread(
        build_preview,
        meta,
        content,
        max_bytes=settings.artifact_preview_max_bytes,
        max_uncompressed_bytes=settings.artifact_preview_max_uncompressed_bytes,
        max_pdf_pages=settings.artifact_preview_max_pdf_pages,
    )


@router.get("/{token}/preview/pages/{page}")
async def get_shared_pdf_page(token: str, page: int):
    share = await get_active_share_by_token(token)
    if not share:
        raise HTTPException(status_code=404, detail="Share not found or revoked")
    meta = await get_artifact(str(share["artifact_id"]))
    if not meta or meta.get("is_deleted"):
        raise HTTPException(status_code=404, detail="Artifact not found")
    if not is_pdf_artifact(meta):
        raise HTTPException(status_code=422, detail="Artifact is not a PDF")
    if int(meta.get("size_bytes") or 0) > settings.artifact_preview_max_bytes:
        raise HTTPException(status_code=413, detail="PDF exceeds the configured preview limit")
    content = await read_artifact_content(str(share["artifact_id"]))
    if content is None:
        raise HTTPException(status_code=404, detail="Content not found")
    if len(content) > settings.artifact_preview_max_bytes:
        raise HTTPException(status_code=413, detail="PDF exceeds the configured preview limit")
    try:
        png = await asyncio.to_thread(
            render_pdf_page,
            content,
            page,
            max_pages=settings.artifact_preview_max_pdf_pages,
        )
    except ArtifactPreviewError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
