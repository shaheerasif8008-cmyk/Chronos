"""Artifact version lifecycle: create new versions (non-destructive), list, read, restore, diff."""
from __future__ import annotations

import difflib
import io
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from core.artifacts import _close_minio, _ensure_bucket, _minio_client, get_artifact
from core.config import settings
from core.db import engine, reflect_table

_MAX_VERSION_ATTEMPTS = 4


async def create_version(
    artifact_id: str,
    content: str | bytes,
    *,
    org_id: str,
    mime_type: str | None = None,
    edit_summary: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Write a new version of an existing artifact without overwriting prior bytes.

    Returns the updated artifact head metadata. Raises ValueError if not found.

    Concurrency: ``next_version`` is derived from the head outside the write
    transaction, so two concurrent edits can compute the same number. The
    ``uq_artifact_version`` constraint rejects the loser; we catch it, re-read
    the head, and retry with a freshly computed version (bounded). The head
    UPDATE is guarded on the observed version so a stale writer never clobbers a
    newer head.
    """
    raw: bytes = content.encode() if isinstance(content, str) else content
    size = len(raw)
    artifacts = await reflect_table("artifacts")
    versions = await reflect_table("artifact_versions")

    last_error: IntegrityError | None = None
    for _ in range(_MAX_VERSION_ATTEMPTS):
        head = await get_artifact(artifact_id)
        if not head or str(head.get("organization_id")) != str(org_id) or head.get("is_deleted"):
            raise ValueError("artifact not found")

        current_version = int(head["version"])
        next_version = current_version + 1
        mime = mime_type or head.get("mime_type") or "text/plain"
        minio_path = f"artifacts/{org_id}/{artifact_id}/v{next_version}"

        # Write bytes outside the DB transaction (never hold a row lock across upload).
        client = None
        try:
            client = await _minio_client()
            await _ensure_bucket(client)
            await client.put_object(
                settings.object_storage_bucket,
                minio_path,
                io.BytesIO(raw),
                length=size,
                content_type=mime,
            )
        except Exception:
            import pathlib as _pl
            _d = _pl.Path("/tmp/chronos_artifacts") / artifact_id
            _d.mkdir(parents=True, exist_ok=True)
            (_d / f"v{next_version}").write_bytes(raw)
            minio_path = f"local://{artifact_id}/v{next_version}"
        finally:
            await _close_minio(client)

        try:
            async with engine.begin() as conn:
                await conn.execute(
                    insert(versions).values(
                        organization_id=org_id,
                        region=head.get("region", "us"),
                        artifact_id=artifact_id,
                        version=next_version,
                        minio_path=minio_path,
                        mime_type=mime,
                        size_bytes=size,
                        edit_summary=edit_summary,
                        created_by=created_by,
                    )
                )
                await conn.execute(
                    update(artifacts)
                    .where(artifacts.c.id == artifact_id, artifacts.c.version == current_version)
                    .values(
                        version=next_version,
                        minio_path=minio_path,
                        mime_type=mime,
                        size_bytes=size,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
            return await get_artifact(artifact_id)
        except IntegrityError as exc:
            last_error = exc  # version collided — recompute and retry
            continue

    raise RuntimeError("artifact version contention: exceeded retry attempts") from last_error


async def list_versions(artifact_id: str, org_id: str) -> list[dict[str, Any]]:
    versions = await reflect_table("artifact_versions")
    async with engine.begin() as conn:
        rows = (await conn.execute(
            select(versions)
            .where(versions.c.artifact_id == artifact_id, versions.c.organization_id == org_id)
            .order_by(versions.c.version.desc())
        )).mappings().all()
    return [dict(r) for r in rows]


async def read_version_content(artifact_id: str, version: int, org_id: str) -> bytes | None:
    versions = await reflect_table("artifact_versions")
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(versions).where(
                versions.c.artifact_id == artifact_id,
                versions.c.version == version,
                versions.c.organization_id == org_id,
            )
        )).mappings().first()
    if not row:
        return None
    path = row["minio_path"]
    client = None
    try:
        client = await _minio_client()
        response = await client.get_object(settings.object_storage_bucket, path)
        return await response.read()
    except Exception:
        import pathlib
        fname = path.split("local://")[-1] if path.startswith("local://") else f"{artifact_id}/v{version}"
        local = pathlib.Path("/tmp/chronos_artifacts") / fname
        return local.read_bytes() if local.exists() else None
    finally:
        await _close_minio(client)


async def restore_version(
    artifact_id: str, version: int, *, org_id: str, created_by: str | None = None
) -> dict[str, Any]:
    """Restore a prior version by writing its content as a NEW version (non-destructive)."""
    content = await read_version_content(artifact_id, version, org_id)
    if content is None:
        raise ValueError("version not found")
    return await create_version(
        artifact_id, content, org_id=org_id,
        edit_summary=f"restored from v{version}", created_by=created_by,
    )


async def diff_versions(artifact_id: str, from_v: int, to_v: int, org_id: str) -> dict[str, Any]:
    """Unified text diff between two versions. Binary content returns is_binary=True."""
    a = await read_version_content(artifact_id, from_v, org_id)
    b = await read_version_content(artifact_id, to_v, org_id)
    if a is None or b is None:
        raise ValueError("version not found")
    try:
        a_text = a.decode("utf-8")
        b_text = b.decode("utf-8")
    except UnicodeDecodeError:
        return {"is_binary": True, "from_version": from_v, "to_version": to_v, "diff": ""}
    diff = "\n".join(difflib.unified_diff(
        a_text.splitlines(), b_text.splitlines(),
        fromfile=f"v{from_v}", tofile=f"v{to_v}", lineterm="",
    ))
    return {"is_binary": False, "from_version": from_v, "to_version": to_v, "diff": diff}
