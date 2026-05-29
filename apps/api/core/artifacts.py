"""Artifact storage: persist agent outputs to MinIO and record metadata in DB.

Usage (from executor, router, or any async context):
    artifact_id = await save_artifact(
        content="# Competitive Brief\n...",
        kind="markdown",
        title="Acme Competitive Analysis",
        conversation_id=...,
        task_id=...,
        org_id=...,
    )
    # Then emit an SSE artifact event so the frontend can render it.
    await emit_activity(task_id, {
        "type": "artifact",
        "artifact_id": artifact_id,
        "kind": "markdown",
        "title": "Acme Competitive Analysis",
    })
"""
from __future__ import annotations

import io
import uuid
from typing import Any

from sqlalchemy import insert, select

from core.config import settings
from core.db import engine, reflect_table


async def _minio_client():
    """Return an async MinIO client (lazy import to avoid hard dep at startup)."""
    from miniopy_async import Minio  # type: ignore[import]

    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


async def _ensure_bucket(client) -> None:
    exists = await client.bucket_exists(settings.minio_bucket)
    if not exists:
        await client.make_bucket(settings.minio_bucket)


async def save_artifact(
    content: str | bytes,
    *,
    kind: str,
    title: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    message_id: str | None = None,
    org_id: str = "default",
    region: str = "us",
    mime_type: str | None = None,
    parent_artifact_id: str | None = None,
    parse_status: str | None = None,
    created_by: str | None = None,
) -> str:
    """Persist an artifact to MinIO and record metadata in the artifacts table.

    Returns the artifact UUID string.
    """
    artifact_id = str(uuid.uuid4())

    # Infer mime_type when not provided.
    if mime_type is None:
        mime_type = _infer_mime(kind, content)

    raw: bytes = content.encode() if isinstance(content, str) else content
    size = len(raw)

    version = 1
    minio_path = f"artifacts/{org_id}/{artifact_id}/v{version}"

    # --- Upload to MinIO ---
    try:
        client = await _minio_client()
        await _ensure_bucket(client)
        await client.put_object(
            settings.minio_bucket,
            minio_path,
            io.BytesIO(raw),
            length=size,
            content_type=mime_type,
        )
    except Exception:
        import pathlib as _pathlib

        _scratch = _pathlib.Path("/tmp/chronos_artifacts") / artifact_id
        _scratch.mkdir(parents=True, exist_ok=True)
        (_scratch / f"v{version}").write_bytes(raw)
        minio_path = f"local://{artifact_id}/v{version}"

    # --- Insert artifact head row + initial version row ---
    artifacts = await reflect_table("artifacts")
    artifact_versions = await reflect_table("artifact_versions")
    async with engine.begin() as conn:
        await conn.execute(
            insert(artifacts).values(
                id=artifact_id,
                organization_id=org_id,
                region=region,
                conversation_id=conversation_id,
                task_id=task_id,
                message_id=message_id,
                kind=kind,
                title=title,
                minio_path=minio_path,
                mime_type=mime_type,
                size_bytes=size,
                version=version,
                parent_artifact_id=parent_artifact_id,
                parse_status=parse_status,
                created_by=created_by,
            )
        )
        await conn.execute(
            insert(artifact_versions).values(
                organization_id=org_id,
                region=region,
                artifact_id=artifact_id,
                version=version,
                minio_path=minio_path,
                mime_type=mime_type,
                size_bytes=size,
                edit_summary="initial",
                created_by=created_by,
            )
        )
    return artifact_id


async def get_artifact(artifact_id: str) -> dict[str, Any] | None:
    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        row = (
            await conn.execute(select(artifacts).where(artifacts.c.id == artifact_id))
        ).mappings().first()
    return dict(row) if row else None


async def read_artifact_content(artifact_id: str) -> bytes | None:
    """Download artifact bytes from MinIO (or local fallback)."""
    meta = await get_artifact(artifact_id)
    if not meta:
        return None
    path: str = meta["minio_path"]

    # Try MinIO first.
    try:
        client = await _minio_client()
        response = await client.get_object(settings.minio_bucket, path)
        return await response.read()
    except Exception:
        pass

    # Local fallback.
    import pathlib

    fname = path.split("local://")[-1] if path.startswith("local://") else f"{artifact_id}_v{meta.get('version', 1)}"
    local = pathlib.Path("/tmp/chronos_artifacts") / fname
    if local.exists():
        return local.read_bytes()
    return None


def _infer_mime(kind: str, content: str | bytes) -> str:
    if kind == "markdown":
        return "text/markdown"
    if kind == "code":
        return "text/plain"
    if kind == "data":
        return "application/json"
    if kind == "file":
        # Guess from content signature.
        if isinstance(content, bytes) and content[:4] == b"%PDF":
            return "application/pdf"
        return "application/octet-stream"
    return "text/plain"


async def _local_fallback(artifact_id: str, raw: bytes, org_id: str) -> str:
    """Write to /tmp when MinIO is unavailable. Returns the pseudo minio_path."""
    import pathlib

    scratch = pathlib.Path("/tmp/chronos_artifacts")
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / artifact_id).write_bytes(raw)
    return f"local://{artifact_id}"


async def set_parse_status(artifact_id: str, status: str) -> None:
    """Update an attachment's parse_status (pending|parsed|failed|unparseable)."""
    from sqlalchemy import update

    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        await conn.execute(
            update(artifacts).where(artifacts.c.id == artifact_id).values(parse_status=status)
        )


async def update_artifact_meta(artifact_id: str, org_id: str, **fields: Any) -> dict[str, Any] | None:
    """Update head metadata (e.g. title). Tenant-scoped. Returns updated row or None."""
    from datetime import datetime
    from sqlalchemy import update as _update

    allowed = {"title", "is_deleted"}
    values = {k: v for k, v in fields.items() if k in allowed}
    if not values:
        return await get_artifact(artifact_id)
    values["updated_at"] = datetime.utcnow()
    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        await conn.execute(
            _update(artifacts)
            .where(artifacts.c.id == artifact_id, artifacts.c.organization_id == org_id)
            .values(**values)
        )
    return await get_artifact(artifact_id)


async def soft_delete_artifact(artifact_id: str, org_id: str) -> bool:
    res = await update_artifact_meta(artifact_id, org_id, is_deleted=True)
    return res is not None
