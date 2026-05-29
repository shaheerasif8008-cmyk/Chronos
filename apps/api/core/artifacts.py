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
import json
import uuid
from typing import Any

from sqlalchemy import and_, insert, or_, select, update

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
    key: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    message_id: str | None = None,
    org_id: str = "default",
    region: str = "us",
    mime_type: str | None = None,
) -> str:
    """Persist an artifact and record metadata in the artifacts table.

    When key is provided, each later write under that key in the same
    conversation/task scope supersedes the prior current row and bumps version.
    Keyless writes stay standalone and self-keyed.
    """
    artifact_id = str(uuid.uuid4())

    # Infer mime_type when not provided.
    if mime_type is None:
        mime_type = _infer_mime(kind, content)

    raw: bytes = content.encode() if isinstance(content, str) else content
    minio_path = await _store_artifact_bytes(artifact_id, raw, org_id, mime_type)
    scope_id = conversation_id or task_id
    current = await _current_artifact_row(org_id, scope_id, key) if key else None
    version = (int(current["version"]) + 1) if current else 1

    values = {
        "id": artifact_id,
        "organization_id": org_id,
        "region": region,
        "conversation_id": conversation_id,
        "task_id": task_id,
        "message_id": message_id,
        "kind": kind,
        "title": title,
        "minio_path": minio_path,
        "mime_type": mime_type,
        "size_bytes": len(raw),
        "artifact_key": key or artifact_id,
        "version": version,
        "is_current": True,
    }
    return await _insert_artifact_version(values, supersede_id=current["id"] if current else None)


async def _store_artifact_bytes(artifact_id: str, raw: bytes, org_id: str, mime_type: str) -> str:
    """Upload bytes to MinIO, falling back to local scratch storage."""
    minio_path = f"artifacts/{org_id}/{artifact_id}"

    try:
        client = await _minio_client()
        await _ensure_bucket(client)
        await client.put_object(
            settings.minio_bucket,
            minio_path,
            io.BytesIO(raw),
            length=len(raw),
            content_type=mime_type,
        )
        return minio_path
    except Exception:
        return await _local_fallback(artifact_id, raw, org_id)


def _scope_clause(artifacts: Any, scope_id: str | None):
    return or_(
        artifacts.c.conversation_id == scope_id,
        and_(artifacts.c.conversation_id.is_(None), artifacts.c.task_id == scope_id),
    )


async def get_current_artifact(org_id: str, scope_id: str | None, key: str) -> dict[str, Any] | None:
    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(artifacts).where(
                    artifacts.c.organization_id == org_id,
                    artifacts.c.artifact_key == key,
                    artifacts.c.is_current.is_(True),
                    _scope_clause(artifacts, scope_id),
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def _current_artifact_row(org_id: str, scope_id: str | None, key: str) -> dict[str, Any] | None:
    return await get_current_artifact(org_id, scope_id, key)


async def _insert_artifact_version(values: dict[str, Any], supersede_id: str | None) -> str:
    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        if supersede_id:
            await conn.execute(
                update(artifacts).where(artifacts.c.id == supersede_id).values(is_current=False)
            )
        result = await conn.execute(insert(artifacts).values(**values).returning(artifacts.c.id))
        return str(result.scalar_one())


async def list_current_artifacts(org_id: str, scope_id: str | None) -> list[dict[str, Any]]:
    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(artifacts)
                .where(
                    artifacts.c.organization_id == org_id,
                    artifacts.c.is_current.is_(True),
                    _scope_clause(artifacts, scope_id),
                )
                .order_by(artifacts.c.created_at.desc())
                .limit(100)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def get_artifact_versions(artifact_id: str) -> list[dict[str, Any]]:
    meta = await get_artifact(artifact_id)
    if not meta:
        return []
    scope_id = meta.get("conversation_id") or meta.get("task_id")
    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(artifacts)
                .where(
                    artifacts.c.organization_id == meta["organization_id"],
                    artifacts.c.artifact_key == meta["artifact_key"],
                    _scope_clause(artifacts, scope_id),
                )
                .order_by(artifacts.c.version.asc())
            )
        ).mappings().all()
    return [dict(row) for row in rows]


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

    local = pathlib.Path("/tmp/chronos_artifacts") / artifact_id
    if local.exists():
        return local.read_bytes()
    return None


def _infer_mime(kind: str, content: str | bytes) -> str:
    if kind == "html":
        return "text/html"
    if kind == "markdown":
        return "text/markdown"
    if kind == "code":
        return "text/plain"
    if kind == "data":
        return "application/json"
    if kind == "image":
        return "image/svg+xml" if isinstance(content, str) and "<svg" in content[:200].lower() else "image/*"
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
