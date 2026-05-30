# Artifact Workspace (Phase 5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade Chronos's persisted artifacts into a Claude-grade artifact workspace: durable creation, non-destructive versioned editing (user + AI), version timeline + diff + restore, type-specific safe renderers, a full artifact browser with grouping/filters and a side panel, and governed publish/share with revocation — all routed through the permission seam and audit log.

**Architecture:** Backend extends the existing `artifacts` table with a dedicated `artifact_versions` table (version-addressed object storage so old bytes survive edits) and an `artifact_shares` table (signed-token public links with revocation). All artifact mutations route through `permission.check` and emit `audit.log`. Publish/unpublish/edit/restore/delete are auditable. The frontend keeps the single-SPA convention but isolates new UI into `apps/web/components/artifacts/` and a shared `apps/web/lib/api.ts`, touching the 3289-line `chat/page.tsx` in exactly one contained task (add route + nav + screen mount).

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy Core + `reflect_table`, Alembic (next revision `0018`), MinIO + local `/tmp` fallback, pytest (DB-guarded async). Next.js 16 (App Router), React 18, TypeScript, Tailwind. Renderers: `marked`-free (hand markdown) is current; use sandboxed `<iframe srcdoc>` + CSP for HTML/SVG/React-as-text.

**Proof strategy (decided with user):** API + build proof. Durable state, versioning, restore, publish/share ACL, permission+audit are proven with DB-backed pytest (using the existing `_requires_db` guard pattern). UI is proven with `npm --prefix apps/web run build` (TypeScript typecheck + Next build) passing. Full Playwright E2E is explicitly out of scope (no harness exists in repo); note this deviation in the final summary.

**Scope bar (explicit assumption):** Rich preview + edit for text-class types (markdown, code, json, csv, html, svg, image-preview). Binary/office/notebook types (pdf, docx, xlsx, pptx, ipynb, zip) get preview-or-download fallback, not bespoke editors. This satisfies the matrix acceptance proofs without building 15 editors.

**Current state (verified):**
- `artifacts` table columns: `id, organization_id, region, conversation_id, task_id, message_id, kind, title, minio_path, mime_type, size_bytes, version (default 1), created_at, parent_artifact_id, parse_status`. (`parent_artifact_id` is already used to link parsed-text children to attachments — DO NOT reuse it for version lineage.)
- `core/artifacts.py`: `save_artifact`, `get_artifact`, `read_artifact_content`, `_infer_mime`, `_local_fallback`, `set_parse_status`. Storage key is `artifacts/{org}/{artifact_id}` (single-key, would clobber on edit).
- `routers/artifacts.py`: `GET /artifacts` (list by convo/task), `GET /artifacts/{id}`, `GET /artifacts/{id}/content`. Raw org compare; no permission seam, no audit.
- Frontend: one SPA `apps/web/app/chat/page.tsx`; other routes re-export it. `Route` type at line 21; nav array ~line 642; route switch ~line 607. `ArtifactCard` at ~line 1531. `apiFetch`/`getToken`/`apiBase` defined inline (~lines 7–17, 287–304). No `apps/web/components/` or `apps/web/lib/` dirs exist yet.

**Seam signatures (use exactly):**
- `await permission.check(actor: Member, action: str, resource: str) -> bool` (`core/permissions.py`, import as `from core import permissions`).
- `await audit.log(event_type, actor_id, action, *, resource_type=None, resource_id=None, payload=None, decision=None) -> str` (`from core import audit`).
- `member: Member = Depends(get_current_member)` (`from core.auth import get_current_member`). `Member` has `.id`, `.organization_id`, `.role`.
- `await reflect_table(name)` + `engine.begin()` (`from core.db import engine, reflect_table`).
- LLM: `await complete_text(prompt, model=None) -> str` (`from core.llm import complete_text`).

---

## File Structure

**Backend (create):**
- `apps/api/migrations/versions/0018_artifact_workspace.py` — versions + shares tables, artifacts columns.
- `apps/api/core/artifact_versions.py` — version create/list/read/restore + diff helper.
- `apps/api/core/artifact_shares.py` — share create/get-by-token/revoke.
- `apps/api/routers/artifact_share.py` — unauthenticated public share read router.
- `apps/api/tests/test_artifact_workspace.py` — backend proof.

**Backend (modify):**
- `apps/api/core/artifacts.py` — version-addressed storage keys; write initial version row on create; add `update_artifact_meta`, soft-delete.
- `apps/api/routers/artifacts.py` — permission+audit on all routes; add edit/version/restore/rename/delete/ai-edit/diff/publish endpoints.
- `apps/api/main.py` — register `artifact_share` router.

**Frontend (create):**
- `apps/web/lib/api.ts` — `apiBase`, `getToken`, `apiFetch` (shared copy for new components).
- `apps/web/lib/artifacts.ts` — typed artifact API client + types.
- `apps/web/components/artifacts/ArtifactRenderer.tsx` — type-specific safe renderers.
- `apps/web/components/artifacts/ArtifactsScreen.tsx` — full browser + side panel + version timeline + diff + editor + publish controls.

**Frontend (modify):**
- `apps/web/app/chat/page.tsx` — one contained edit: add `"artifacts"` to `Route`, nav item, route switch mount.

---

## Task 1: Schema migration + version-addressed storage

**Files:**
- Create: `apps/api/migrations/versions/0018_artifact_workspace.py`
- Modify: `apps/api/core/artifacts.py`
- Test: `apps/api/tests/test_artifact_workspace.py`

- [ ] **Step 1: Write the migration**

Create `apps/api/migrations/versions/0018_artifact_workspace.py`:

```python
"""artifact workspace: versions, shares, artifact metadata columns

Revision ID: 0018_artifact_workspace
Revises: 0017_attachment_parsing
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_artifact_workspace"
down_revision = "0017_attachment_parsing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- artifacts: workspace metadata ---
    op.add_column("artifacts", sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")))
    op.add_column("artifacts", sa.Column("created_by", sa.Text(), nullable=True))
    op.add_column("artifacts", sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    # --- artifact_versions: one row per saved version, version-addressed bytes ---
    op.create_table(
        "artifact_versions",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("artifact_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("minio_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("edit_summary", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_id", "version", name="uq_artifact_version"),
    )
    op.create_index("ix_artifact_versions_artifact", "artifact_versions", ["artifact_id", "version"])

    # --- artifact_shares: signed-token public links with revocation ---
    op.create_table(
        "artifact_shares",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("artifact_id", sa.UUID(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=False, server_default="public_link"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_artifact_share_token"),
    )
    op.create_index("ix_artifact_shares_artifact", "artifact_shares", ["artifact_id"])


def downgrade() -> None:
    op.drop_index("ix_artifact_shares_artifact", "artifact_shares")
    op.drop_table("artifact_shares")
    op.drop_index("ix_artifact_versions_artifact", "artifact_versions")
    op.drop_table("artifact_versions")
    op.drop_column("artifacts", "is_deleted")
    op.drop_column("artifacts", "created_by")
    op.drop_column("artifacts", "updated_at")
```

- [ ] **Step 2: Update `core/artifacts.py` to version-addressed keys + initial version row**

In `save_artifact`, change the storage key from `f"artifacts/{org_id}/{artifact_id}"` to a version-addressed key and write the first `artifact_versions` row. Replace the body from the `minio_path = ...` line through the metadata insert with:

```python
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
        minio_path = await _local_fallback(f"{artifact_id}_v{version}", raw, org_id)

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
```

Also add a `created_by: str | None = None` keyword parameter to `save_artifact`'s signature (after `parse_status`). And update `_local_fallback` to accept the composite key (it already takes `artifact_id` first positional — pass `f"{artifact_id}_v{version}"`; the function writes to `scratch / artifact_id`, so it already keys by the string passed). Update `read_artifact_content`'s local fallback to try `local / meta["minio_path"].split("/")[-1]`-style: since `minio_path` now ends with `.../v{n}` and the local file is named `{artifact_id}_v{n}`, change the local fallback in `read_artifact_content` to:

```python
    # Local fallback.
    import pathlib

    fname = path.split("local://")[-1] if path.startswith("local://") else f"{artifact_id}_v{meta.get('version', 1)}"
    local = pathlib.Path("/tmp/chronos_artifacts") / fname
    if local.exists():
        return local.read_bytes()
    return None
```

(Keep MinIO path as the primary read; local fallback is only for no-MinIO environments.)

- [ ] **Step 3: Write the failing test**

Create `apps/api/tests/test_artifact_workspace.py`:

```python
import os
import socket
import uuid

import pytest


def _db_reachable() -> bool:
    host, _, port_str = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://chronos:chronos@localhost:5432/chronos"
    ).rpartition("@")[-1].partition("/")[0].rpartition(":")
    try:
        with socket.create_connection((host or "localhost", int(port_str or 5432)), timeout=1):
            return True
    except OSError:
        return False


_requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable")


@_requires_db
@pytest.mark.asyncio
async def test_save_artifact_writes_head_and_initial_version():
    from core.artifacts import get_artifact, read_artifact_content, save_artifact
    from core.db import engine, reflect_table
    from sqlalchemy import select

    org = f"test-{uuid.uuid4().hex[:8]}"
    aid = await save_artifact(
        "# Hello\nWorld", kind="markdown", title="T1", org_id=org, created_by="member:tester"
    )
    meta = await get_artifact(aid)
    assert meta is not None
    assert meta["version"] == 1
    assert meta["created_by"] == "member:tester"
    assert meta["minio_path"].endswith("/v1")
    content = await read_artifact_content(aid)
    assert content == b"# Hello\nWorld"

    versions = await reflect_table("artifact_versions")
    async with engine.begin() as conn:
        rows = (await conn.execute(
            select(versions).where(versions.c.artifact_id == aid)
        )).mappings().all()
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    assert rows[0]["edit_summary"] == "initial"
```

- [ ] **Step 4: Run migration then test**

Run: `cd apps/api && alembic upgrade head && python -m pytest tests/test_artifact_workspace.py -v`
Expected: migration applies; test PASSES if Postgres reachable, else SKIPPED. If Postgres is not reachable in this environment, the test is skipped — that is acceptable; still confirm `python -c "import core.artifacts"` imports cleanly and `alembic upgrade head` is dry-run-checked via `alembic check` or by importing the migration module: `python -c "import importlib.util; importlib.util.spec_from_file_location('m','migrations/versions/0018_artifact_workspace.py')"`.

- [ ] **Step 5: Commit**

```bash
git add apps/api/migrations/versions/0018_artifact_workspace.py apps/api/core/artifacts.py apps/api/tests/test_artifact_workspace.py
git commit -m "feat(artifacts): version-addressed storage + versions/shares schema (0018)"
```

---

## Task 2: Versioning, editing, restore, diff, rename, soft-delete — backend

**Files:**
- Create: `apps/api/core/artifact_versions.py`
- Modify: `apps/api/core/artifacts.py`, `apps/api/routers/artifacts.py`
- Test: `apps/api/tests/test_artifact_workspace.py`

- [ ] **Step 1: Create `core/artifact_versions.py`**

```python
"""Artifact version lifecycle: create new versions (non-destructive), list, read, restore, diff."""
from __future__ import annotations

import difflib
import io
from typing import Any

from sqlalchemy import insert, select, update

from core.artifacts import _ensure_bucket, _local_fallback, _minio_client, get_artifact
from core.config import settings
from core.db import engine, reflect_table


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
    """
    head = await get_artifact(artifact_id)
    if not head or str(head.get("organization_id")) != str(org_id) or head.get("is_deleted"):
        raise ValueError("artifact not found")

    next_version = int(head["version"]) + 1
    raw: bytes = content.encode() if isinstance(content, str) else content
    size = len(raw)
    mime = mime_type or head.get("mime_type") or "text/plain"
    minio_path = f"artifacts/{org_id}/{artifact_id}/v{next_version}"

    try:
        client = await _minio_client()
        await _ensure_bucket(client)
        await client.put_object(
            settings.minio_bucket, minio_path, io.BytesIO(raw), length=size, content_type=mime
        )
    except Exception:
        minio_path = await _local_fallback(f"{artifact_id}_v{next_version}", raw, org_id)

    artifacts = await reflect_table("artifacts")
    versions = await reflect_table("artifact_versions")
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
            .where(artifacts.c.id == artifact_id)
            .values(
                version=next_version,
                minio_path=minio_path,
                mime_type=mime,
                size_bytes=size,
                updated_at=__import__("datetime").datetime.utcnow(),
            )
        )
    return await get_artifact(artifact_id)


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
    try:
        client = await _minio_client()
        response = await client.get_object(settings.minio_bucket, path)
        return await response.read()
    except Exception:
        import pathlib

        fname = path.split("local://")[-1] if path.startswith("local://") else f"{artifact_id}_v{version}"
        local = pathlib.Path("/tmp/chronos_artifacts") / fname
        return local.read_bytes() if local.exists() else None


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
```

- [ ] **Step 2: Add `update_artifact_meta` + `soft_delete_artifact` to `core/artifacts.py`**

Append to `core/artifacts.py`:

```python
async def update_artifact_meta(artifact_id: str, org_id: str, **fields: Any) -> dict[str, Any] | None:
    """Update head metadata (e.g. title). Tenant-scoped. Returns updated row or None."""
    from sqlalchemy import update as _update

    allowed = {"title", "is_deleted"}
    values = {k: v for k, v in fields.items() if k in allowed}
    if not values:
        return await get_artifact(artifact_id)
    values["updated_at"] = __import__("datetime").datetime.utcnow()
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
```

- [ ] **Step 3: Rewrite `routers/artifacts.py` with seams + workspace endpoints**

Replace the entire file with:

```python
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
    await audit.log("artifact", member.id, "artifact.create", resource_type="artifact", resource_id=aid)
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
    await audit.log("artifact", member.id, "artifact.edit", resource_type="artifact",
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
    await audit.log("artifact", member.id, "artifact.ai_edit", resource_type="artifact",
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
    await audit.log("artifact", member.id, "artifact.restore", resource_type="artifact",
                    resource_id=artifact_id, payload={"restored_from": version, "version": updated["version"]})
    return updated


@router.patch("/{artifact_id}")
async def rename_artifact(artifact_id: str, body: RenameBody, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.edit", artifact_id)
    updated = await update_artifact_meta(artifact_id, member.organization_id, title=body.title)
    await audit.log("artifact", member.id, "artifact.rename", resource_type="artifact",
                    resource_id=artifact_id, payload={"title": body.title})
    return updated


@router.delete("/{artifact_id}")
async def delete_artifact(artifact_id: str, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.delete", artifact_id)
    await soft_delete_artifact(artifact_id, member.organization_id)
    await audit.log("artifact", member.id, "artifact.delete", resource_type="artifact", resource_id=artifact_id)
    return {"ok": True}
```

- [ ] **Step 4: Append tests for versioning/edit/restore/diff/rename/delete**

Add to `apps/api/tests/test_artifact_workspace.py`:

```python
@_requires_db
@pytest.mark.asyncio
async def test_edit_creates_new_version_without_clobbering():
    from core.artifacts import read_artifact_content, save_artifact
    from core.artifact_versions import create_version, read_version_content, restore_version, diff_versions

    org = f"test-{uuid.uuid4().hex[:8]}"
    aid = await save_artifact("v1 body", kind="markdown", title="E", org_id=org)
    updated = await create_version(aid, "v2 body", org_id=org, edit_summary="edit")
    assert updated["version"] == 2
    # old bytes survive
    assert await read_version_content(aid, 1, org) == b"v1 body"
    assert await read_version_content(aid, 2, org) == b"v2 body"
    # head points at latest
    assert await read_artifact_content(aid) == b"v2 body"
    # diff
    d = await diff_versions(aid, 1, 2, org)
    assert d["is_binary"] is False
    assert "v1 body" in d["diff"] and "v2 body" in d["diff"]
    # restore writes a NEW version with v1 content
    restored = await restore_version(aid, 1, org_id=org)
    assert restored["version"] == 3
    assert await read_artifact_content(aid) == b"v1 body"


@_requires_db
@pytest.mark.asyncio
async def test_cross_tenant_version_isolation():
    from core.artifacts import save_artifact
    from core.artifact_versions import read_version_content

    org_a = f"a-{uuid.uuid4().hex[:8]}"
    org_b = f"b-{uuid.uuid4().hex[:8]}"
    aid = await save_artifact("secret", kind="markdown", title="X", org_id=org_a)
    # reading version under the wrong org returns None
    assert await read_version_content(aid, 1, org_b) is None
    assert await read_version_content(aid, 1, org_a) == b"secret"
```

- [ ] **Step 5: Run tests + import check**

Run: `cd apps/api && python -c "import routers.artifacts, core.artifact_versions" && python -m pytest tests/test_artifact_workspace.py -v`
Expected: imports clean; tests PASS (or SKIP if no Postgres).

- [ ] **Step 6: Commit**

```bash
git add apps/api/core/artifact_versions.py apps/api/core/artifacts.py apps/api/routers/artifacts.py apps/api/tests/test_artifact_workspace.py
git commit -m "feat(artifacts): versioning, edit, AI-edit, restore, diff, rename, delete with seams"
```

---

## Task 3: Publish/share with revocation — backend

**Files:**
- Create: `apps/api/core/artifact_shares.py`, `apps/api/routers/artifact_share.py`
- Modify: `apps/api/routers/artifacts.py`, `apps/api/main.py`
- Test: `apps/api/tests/test_artifact_workspace.py`

- [ ] **Step 1: Create `core/artifact_shares.py`**

```python
"""Artifact publish/share: signed-token public links with revocation."""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, update

from core.db import engine, reflect_table


async def create_share(artifact_id: str, *, org_id: str, created_by: str | None = None) -> dict[str, Any]:
    """Create (or reactivate) a public share link for an artifact. Returns the share row."""
    shares = await reflect_table("artifact_shares")
    async with engine.begin() as conn:
        existing = (await conn.execute(
            select(shares).where(
                shares.c.artifact_id == artifact_id,
                shares.c.organization_id == org_id,
                shares.c.status == "active",
            )
        )).mappings().first()
        if existing:
            return dict(existing)
        token = secrets.token_urlsafe(24)
        row = (await conn.execute(
            insert(shares).values(
                organization_id=org_id,
                artifact_id=artifact_id,
                token=token,
                visibility="public_link",
                status="active",
                created_by=created_by,
            ).returning(shares)
        )).mappings().first()
    return dict(row)


async def get_active_share_by_token(token: str) -> dict[str, Any] | None:
    shares = await reflect_table("artifact_shares")
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(shares).where(shares.c.token == token, shares.c.status == "active")
        )).mappings().first()
    return dict(row) if row else None


async def get_share_for_artifact(artifact_id: str, org_id: str) -> dict[str, Any] | None:
    shares = await reflect_table("artifact_shares")
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(shares).where(
                shares.c.artifact_id == artifact_id,
                shares.c.organization_id == org_id,
                shares.c.status == "active",
            )
        )).mappings().first()
    return dict(row) if row else None


async def revoke_share(artifact_id: str, org_id: str) -> bool:
    shares = await reflect_table("artifact_shares")
    async with engine.begin() as conn:
        res = await conn.execute(
            update(shares)
            .where(
                shares.c.artifact_id == artifact_id,
                shares.c.organization_id == org_id,
                shares.c.status == "active",
            )
            .values(status="revoked", revoked_at=datetime.utcnow())
        )
    return res.rowcount > 0
```

- [ ] **Step 2: Create public read router `routers/artifact_share.py`**

This router is UNAUTHENTICATED (public link) and only serves artifacts with an active share token. It must never accept an artifact_id directly — only a token.

```python
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
```

- [ ] **Step 3: Add publish/unpublish/share-status endpoints to `routers/artifacts.py`**

Add these imports at the top of `routers/artifacts.py`:

```python
from core.artifact_shares import create_share, get_share_for_artifact, revoke_share
```

Add these endpoints to the router (after `delete_artifact`):

```python
@router.post("/{artifact_id}/publish")
async def publish_artifact(artifact_id: str, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.publish", artifact_id)
    share = await create_share(artifact_id, org_id=member.organization_id, created_by=f"member:{member.id}")
    await audit.log("artifact", member.id, "artifact.publish", resource_type="artifact",
                    resource_id=artifact_id, decision="published")
    return {"token": share["token"], "status": share["status"], "share_path": f"/shared/{share['token']}"}


@router.post("/{artifact_id}/unpublish")
async def unpublish_artifact(artifact_id: str, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.publish", artifact_id)
    revoked = await revoke_share(artifact_id, member.organization_id)
    await audit.log("artifact", member.id, "artifact.unpublish", resource_type="artifact",
                    resource_id=artifact_id, decision="revoked")
    return {"revoked": revoked}


@router.get("/{artifact_id}/share")
async def share_status(artifact_id: str, member: Member = Depends(get_current_member)):
    await _require(member, "artifact.read", artifact_id)
    share = await get_share_for_artifact(artifact_id, member.organization_id)
    if not share:
        return {"published": False}
    return {"published": True, "token": share["token"], "share_path": f"/shared/{share['token']}"}
```

- [ ] **Step 4: Register the public router in `main.py`**

In `apps/api/main.py`, add `artifact_share` to the `from routers import ...` line and add `app.include_router(artifact_share.router)` next to the other `include_router` calls.

- [ ] **Step 5: Append publish/revoke tests**

Add to `apps/api/tests/test_artifact_workspace.py`:

```python
@_requires_db
@pytest.mark.asyncio
async def test_publish_then_unpublish_revokes_token():
    from core.artifacts import save_artifact
    from core.artifact_shares import (
        create_share, get_active_share_by_token, revoke_share,
    )

    org = f"test-{uuid.uuid4().hex[:8]}"
    aid = await save_artifact("public doc", kind="markdown", title="P", org_id=org)
    share = await create_share(aid, org_id=org)
    token = share["token"]
    assert await get_active_share_by_token(token) is not None
    # idempotent: re-publish returns same active token
    assert (await create_share(aid, org_id=org))["token"] == token
    # unpublish revokes — token no longer resolves
    assert await revoke_share(aid, org) is True
    assert await get_active_share_by_token(token) is None


@_requires_db
@pytest.mark.asyncio
async def test_revoke_when_not_published_is_false():
    from core.artifacts import save_artifact
    from core.artifact_shares import revoke_share

    org = f"test-{uuid.uuid4().hex[:8]}"
    aid = await save_artifact("doc", kind="markdown", title="Q", org_id=org)
    assert await revoke_share(aid, org) is False
```

- [ ] **Step 6: Run tests + import check**

Run: `cd apps/api && python -c "import main, routers.artifact_share, core.artifact_shares" && python -m pytest tests/test_artifact_workspace.py -v`
Expected: imports clean; tests PASS (or SKIP without Postgres).

- [ ] **Step 7: Commit**

```bash
git add apps/api/core/artifact_shares.py apps/api/routers/artifact_share.py apps/api/routers/artifacts.py apps/api/main.py apps/api/tests/test_artifact_workspace.py
git commit -m "feat(artifacts): governed publish/share with token revocation"
```

---

## Task 4: Frontend shared API lib + typed artifact client

**Files:**
- Create: `apps/web/lib/api.ts`, `apps/web/lib/artifacts.ts`

- [ ] **Step 1: Create `apps/web/lib/api.ts`**

Mirror the inline helpers from `chat/page.tsx` (lines 7–17, 287–304) so new components share one source:

```typescript
const CONFIGURED_API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL;

export function apiBase(): string {
  if (CONFIGURED_API_BASE) return CONFIGURED_API_BASE;
  if (typeof window !== "undefined") {
    const host = window.location.hostname;
    if (host && host !== "localhost" && host !== "127.0.0.1") {
      return `${window.location.protocol}//${host.replace(/^app\./, "api.")}`;
    }
  }
  return "http://localhost:8000";
}

export function getToken(): string {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("chronos_token") ?? "";
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const res = await fetch(`${apiBase()}${path}`, { ...init, headers });
  if (res.status === 401 && typeof window !== "undefined") {
    localStorage.removeItem("chronos_token");
    window.location.href = "/login";
  }
  if (!res.ok) throw new Error(await res.text());
  return res;
}
```

(Note: verify lines 7–17 of `chat/page.tsx` for the exact `apiBase` body and copy it verbatim so behavior matches. If the inline version differs, the inline version wins.)

- [ ] **Step 2: Create `apps/web/lib/artifacts.ts`**

```typescript
import { apiFetch } from "./api";

export type Artifact = {
  id: string;
  title: string | null;
  kind: string;
  mime_type: string | null;
  size_bytes: number | null;
  version: number;
  conversation_id: string | null;
  task_id: string | null;
  created_at: string;
  updated_at?: string | null;
};

export type ArtifactVersion = {
  id: string;
  version: number;
  mime_type: string | null;
  size_bytes: number | null;
  edit_summary: string | null;
  created_by: string | null;
  created_at: string;
};

export type DiffResult = { is_binary: boolean; from_version: number; to_version: number; diff: string };

export async function listArtifacts(params: { conversation_id?: string; kind?: string } = {}): Promise<Artifact[]> {
  const q = new URLSearchParams();
  if (params.conversation_id) q.set("conversation_id", params.conversation_id);
  if (params.kind) q.set("kind", params.kind);
  const res = await apiFetch(`/artifacts${q.toString() ? `?${q}` : ""}`);
  return res.json();
}

export async function getArtifact(id: string): Promise<Artifact> {
  return (await apiFetch(`/artifacts/${id}`)).json();
}

export async function getContentText(id: string): Promise<string> {
  return (await apiFetch(`/artifacts/${id}/content`)).text();
}

export async function getContentBlob(id: string): Promise<Blob> {
  return (await apiFetch(`/artifacts/${id}/content`)).blob();
}

export async function listVersions(id: string): Promise<ArtifactVersion[]> {
  return (await apiFetch(`/artifacts/${id}/versions`)).json();
}

export async function getVersionText(id: string, version: number): Promise<string> {
  return (await apiFetch(`/artifacts/${id}/versions/${version}/content`)).text();
}

export async function getDiff(id: string, from_version: number, to_version: number): Promise<DiffResult> {
  return (await apiFetch(`/artifacts/${id}/diff?from_version=${from_version}&to_version=${to_version}`)).json();
}

export async function editArtifact(id: string, content: string, edit_summary?: string): Promise<Artifact> {
  return (await apiFetch(`/artifacts/${id}/edit`, { method: "POST", body: JSON.stringify({ content, edit_summary }) })).json();
}

export async function aiEditArtifact(id: string, instruction: string): Promise<Artifact> {
  return (await apiFetch(`/artifacts/${id}/ai-edit`, { method: "POST", body: JSON.stringify({ instruction }) })).json();
}

export async function restoreVersion(id: string, version: number): Promise<Artifact> {
  return (await apiFetch(`/artifacts/${id}/restore/${version}`, { method: "POST" })).json();
}

export async function renameArtifact(id: string, title: string): Promise<Artifact> {
  return (await apiFetch(`/artifacts/${id}`, { method: "PATCH", body: JSON.stringify({ title }) })).json();
}

export async function deleteArtifact(id: string): Promise<void> {
  await apiFetch(`/artifacts/${id}`, { method: "DELETE" });
}

export async function getShareStatus(id: string): Promise<{ published: boolean; token?: string; share_path?: string }> {
  return (await apiFetch(`/artifacts/${id}/share`)).json();
}

export async function publishArtifact(id: string): Promise<{ token: string; share_path: string }> {
  return (await apiFetch(`/artifacts/${id}/publish`, { method: "POST" })).json();
}

export async function unpublishArtifact(id: string): Promise<{ revoked: boolean }> {
  return (await apiFetch(`/artifacts/${id}/unpublish`, { method: "POST" })).json();
}
```

- [ ] **Step 3: Build proof**

Run: `cd apps/web && npm run build`
Expected: build succeeds (TypeScript compiles the new lib files; no usage yet is fine since Next tree-shakes unused modules — they must still typecheck).

- [ ] **Step 4: Commit**

```bash
git add apps/web/lib/api.ts apps/web/lib/artifacts.ts
git commit -m "feat(web): shared api lib + typed artifact client"
```

---

## Task 5: Artifact renderers component (safe, type-specific)

**Files:**
- Create: `apps/web/components/artifacts/ArtifactRenderer.tsx`

- [ ] **Step 1: Create `ArtifactRenderer.tsx`**

Renders by kind/mime. HTML and SVG render in a sandboxed iframe (`sandbox` without `allow-same-origin`/`allow-scripts` for SVG; HTML gets `allow-scripts` only inside a fully sandboxed, srcdoc iframe with a restrictive CSP meta). React/code/json/markdown render as syntax-styled text. Images render via blob URL. Unknown/binary types show a download fallback.

```tsx
"use client";
import { useEffect, useMemo, useRef, useState } from "react";

type Props = {
  kind: string;
  mimeType: string | null;
  content: string | null;   // text content (null for binary)
  blobUrl?: string | null;  // for images/binary preview
  title?: string | null;
};

function classifyRenderer(kind: string, mime: string | null): string {
  const m = (mime ?? "").toLowerCase();
  if (m.startsWith("image/")) return "image";
  if (m.includes("svg")) return "svg";
  if (m.startsWith("text/html") || kind === "html") return "html";
  if (m.includes("json") || kind === "data") return "json";
  if (m.includes("csv") || kind === "csv") return "csv";
  if (kind === "markdown" || m.includes("markdown")) return "markdown";
  if (kind === "code" || m.startsWith("text/")) return "code";
  // react components are persisted as code; treat as code preview
  if (kind === "react") return "code";
  return "download";
}

const SANDBOX_CSP =
  '<meta http-equiv="Content-Security-Policy" ' +
  "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:;\">";

export function ArtifactRenderer({ kind, mimeType, content, blobUrl, title }: Props) {
  const renderer = useMemo(() => classifyRenderer(kind, mimeType), [kind, mimeType]);

  if (renderer === "image" && blobUrl) {
    return <img src={blobUrl} alt={title ?? "artifact"} className="max-w-full rounded-lg" />;
  }
  if (renderer === "svg" && content) {
    // Sandboxed, no scripts, no same-origin — SVG cannot execute or call out.
    const srcdoc = `<!doctype html><html><head>${SANDBOX_CSP}</head><body style="margin:0">${content}</body></html>`;
    return <iframe sandbox="" srcDoc={srcdoc} className="w-full min-h-[320px] rounded-lg border" title="svg" />;
  }
  if (renderer === "html" && content) {
    // Fully sandboxed; scripts allowed but no same-origin, no top navigation, CSP blocks network.
    const srcdoc = content.includes("Content-Security-Policy")
      ? content
      : `<!doctype html><html><head>${SANDBOX_CSP.replace("default-src 'none'", "default-src 'none'; script-src 'unsafe-inline'")}</head><body>${content}</body></html>`;
    return <iframe sandbox="allow-scripts" srcDoc={srcdoc} className="w-full min-h-[320px] rounded-lg border" title="html" />;
  }
  if (renderer === "json" && content) {
    let pretty = content;
    try { pretty = JSON.stringify(JSON.parse(content), null, 2); } catch { /* show raw */ }
    return <pre className="text-[12.5px] overflow-auto p-3 rounded-lg" style={{ background: "var(--surface)" }}>{pretty}</pre>;
  }
  if (renderer === "csv" && content) {
    return <CsvTable content={content} />;
  }
  if (renderer === "markdown" && content) {
    return <pre className="whitespace-pre-wrap text-[13.5px] leading-relaxed p-1">{content}</pre>;
  }
  if (renderer === "code" && content) {
    return <pre className="text-[12.5px] overflow-auto p-3 rounded-lg font-mono" style={{ background: "var(--surface)" }}>{content}</pre>;
  }
  return (
    <div className="text-[13px] p-4 rounded-lg" style={{ background: "var(--surface)", color: "var(--text-dim)" }}>
      No inline preview for this type{mimeType ? ` (${mimeType})` : ""}. Use Download to open it.
    </div>
  );
}

function CsvTable({ content }: { content: string }) {
  const rows = useMemo(() => {
    return content.trim().split(/\r?\n/).slice(0, 200).map((line) => {
      // minimal CSV split (handles simple quoted cells)
      const cells: string[] = [];
      let cur = "", inQ = false;
      for (const ch of line) {
        if (ch === '"') inQ = !inQ;
        else if (ch === "," && !inQ) { cells.push(cur); cur = ""; }
        else cur += ch;
      }
      cells.push(cur);
      return cells;
    });
  }, [content]);
  if (!rows.length) return null;
  const [head, ...body] = rows;
  return (
    <div className="overflow-auto rounded-lg border" style={{ borderColor: "var(--border)" }}>
      <table className="text-[12.5px] w-full border-collapse">
        <thead>
          <tr>{head.map((h, i) => <th key={i} className="text-left px-2 py-1.5 font-semibold border-b" style={{ borderColor: "var(--border)", background: "var(--surface)" }}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {body.map((r, ri) => <tr key={ri}>{r.map((c, ci) => <td key={ci} className="px-2 py-1 border-b" style={{ borderColor: "var(--border)" }}>{c}</td>)}</tr>)}
        </tbody>
      </table>
    </div>
  );
}

// expose for tests / reuse
export { classifyRenderer };
```

- [ ] **Step 2: Build proof**

Run: `cd apps/web && npm run build`
Expected: build succeeds.

- [ ] **Step 3: Commit**

```bash
git add apps/web/components/artifacts/ArtifactRenderer.tsx
git commit -m "feat(web): safe type-specific artifact renderers"
```

---

## Task 6: Artifact workspace screen (browser + side panel + versions + diff + editor + publish) and SPA wiring

**Files:**
- Create: `apps/web/components/artifacts/ArtifactsScreen.tsx`
- Modify: `apps/web/app/chat/page.tsx` (one contained edit)

- [ ] **Step 1: Create `ArtifactsScreen.tsx`**

A two-pane workspace: left = filterable artifact list (grouped by conversation/task), right = selected artifact with tabs Preview / Edit / Versions. Uses `lib/artifacts.ts` + `ArtifactRenderer`. Implements rename, delete, manual edit (creates version), AI edit, version timeline with restore, diff viewer (latest vs selected), and publish/unpublish with copyable link.

```tsx
"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { apiBase } from "../../lib/api";
import {
  Artifact, ArtifactVersion, DiffResult,
  aiEditArtifact, deleteArtifact, editArtifact, getArtifact, getContentBlob, getContentText,
  getDiff, getShareStatus, getVersionText, listArtifacts, listVersions,
  publishArtifact, renameArtifact, restoreVersion, unpublishArtifact,
} from "../../lib/artifacts";
import { ArtifactRenderer } from "./ArtifactRenderer";

type Tab = "preview" | "edit" | "versions";

export default function ArtifactsScreen() {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<string>("");
  const [query, setQuery] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    try { setArtifacts(await listArtifacts(kindFilter ? { kind: kindFilter } : {})); }
    finally { setLoading(false); }
  }, [kindFilter]);

  useEffect(() => { void refresh(); }, [refresh]);

  const filtered = useMemo(() => {
    const q = query.toLowerCase();
    return artifacts.filter(a => !q || (a.title ?? "").toLowerCase().includes(q) || a.kind.includes(q));
  }, [artifacts, query]);

  const kinds = useMemo(() => Array.from(new Set(artifacts.map(a => a.kind))).sort(), [artifacts]);

  return (
    <div className="flex h-full min-h-0">
      <aside className="w-[320px] flex-shrink-0 border-r flex flex-col" style={{ borderColor: "var(--border)" }}>
        <div className="p-3 border-b flex flex-col gap-2" style={{ borderColor: "var(--border)" }}>
          <div className="text-[15px] font-semibold">Artifacts</div>
          <input value={query} onChange={e => setQuery(e.target.value)} placeholder="Search artifacts…"
                 className="w-full px-2.5 py-1.5 rounded-lg border text-[13px]" style={{ borderColor: "var(--border)", background: "var(--surface)" }} />
          <select value={kindFilter} onChange={e => setKindFilter(e.target.value)}
                  className="w-full px-2 py-1.5 rounded-lg border text-[13px]" style={{ borderColor: "var(--border)", background: "var(--surface)" }}>
            <option value="">All types</option>
            {kinds.map(k => <option key={k} value={k}>{k}</option>)}
          </select>
        </div>
        <div className="flex-1 overflow-auto p-2">
          {loading && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>Loading…</div>}
          {!loading && filtered.length === 0 && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>No artifacts yet.</div>}
          {filtered.map(a => (
            <button key={a.id} onClick={() => setSelectedId(a.id)}
                    className={`w-full text-left px-3 py-2 rounded-lg mb-1 ${selectedId === a.id ? "active" : ""}`}
                    style={{ background: selectedId === a.id ? "var(--accent-soft)" : "transparent" }}>
              <div className="text-[13.5px] font-medium truncate">{a.title ?? "Untitled"}</div>
              <div className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{a.kind} · v{a.version}</div>
            </button>
          ))}
        </div>
      </aside>
      <section className="flex-1 min-w-0">
        {selectedId
          ? <ArtifactDetail key={selectedId} artifactId={selectedId} onChanged={refresh} onDeleted={() => { setSelectedId(null); void refresh(); }} />
          : <div className="h-full flex items-center justify-center text-[14px]" style={{ color: "var(--text-dim)" }}>Select an artifact to preview, edit, version, or publish.</div>}
      </section>
    </div>
  );
}

function ArtifactDetail({ artifactId, onChanged, onDeleted }: { artifactId: string; onChanged: () => void; onDeleted: () => void }) {
  const [meta, setMeta] = useState<Artifact | null>(null);
  const [tab, setTab] = useState<Tab>("preview");
  const [text, setText] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [versions, setVersions] = useState<ArtifactVersion[]>([]);
  const [diff, setDiff] = useState<DiffResult | null>(null);
  const [share, setShare] = useState<{ published: boolean; share_path?: string }>({ published: false });
  const [aiInstruction, setAiInstruction] = useState("");
  const [busy, setBusy] = useState(false);

  const isText = useMemo(() => {
    const m = (meta?.mime_type ?? "").toLowerCase();
    return !m.startsWith("image/") && !m.includes("octet-stream") && !m.includes("pdf");
  }, [meta]);

  const load = useCallback(async () => {
    const m = await getArtifact(artifactId);
    setMeta(m);
    setShare(await getShareStatus(artifactId));
    const mime = (m.mime_type ?? "").toLowerCase();
    if (mime.startsWith("image/")) {
      const blob = await getContentBlob(artifactId);
      setBlobUrl(URL.createObjectURL(blob));
      setText(null);
    } else {
      try { const t = await getContentText(artifactId); setText(t); setDraft(t); } catch { setText(null); }
    }
  }, [artifactId]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => () => { if (blobUrl) URL.revokeObjectURL(blobUrl); }, [blobUrl]);

  const loadVersions = useCallback(async () => {
    const vs = await listVersions(artifactId);
    setVersions(vs);
    if (vs.length >= 2) setDiff(await getDiff(artifactId, vs[1].version, vs[0].version));
    else setDiff(null);
  }, [artifactId]);

  useEffect(() => { if (tab === "versions") void loadVersions(); }, [tab, loadVersions]);

  async function save() { setBusy(true); try { await editArtifact(artifactId, draft, "manual edit"); await load(); onChanged(); setTab("preview"); } finally { setBusy(false); } }
  async function aiEdit() { if (!aiInstruction.trim()) return; setBusy(true); try { await aiEditArtifact(artifactId, aiInstruction); setAiInstruction(""); await load(); onChanged(); setTab("preview"); } finally { setBusy(false); } }
  async function restore(v: number) { setBusy(true); try { await restoreVersion(artifactId, v); await load(); await loadVersions(); onChanged(); } finally { setBusy(false); } }
  async function rename() { const t = prompt("Rename artifact", meta?.title ?? ""); if (t != null) { await renameArtifact(artifactId, t); await load(); onChanged(); } }
  async function remove() { if (confirm("Delete this artifact?")) { await deleteArtifact(artifactId); onDeleted(); } }
  async function togglePublish() {
    setBusy(true);
    try {
      if (share.published) { await unpublishArtifact(artifactId); }
      else { await publishArtifact(artifactId); }
      setShare(await getShareStatus(artifactId));
    } finally { setBusy(false); }
  }

  if (!meta) return <div className="p-6 text-[14px]" style={{ color: "var(--text-dim)" }}>Loading…</div>;
  const shareUrl = share.share_path ? `${apiBase()}${share.share_path}` : "";

  return (
    <div className="flex flex-col h-full min-h-0">
      <header className="px-5 py-3 border-b flex items-center gap-3" style={{ borderColor: "var(--border)" }}>
        <div className="flex-1 min-w-0">
          <div className="text-[15px] font-semibold truncate">{meta.title ?? "Untitled"}</div>
          <div className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{meta.kind} · v{meta.version}{meta.mime_type ? ` · ${meta.mime_type}` : ""}</div>
        </div>
        <button onClick={rename} className="btn btn-ghost btn-sm">Rename</button>
        <button onClick={togglePublish} disabled={busy} className="btn btn-secondary btn-sm">{share.published ? "Unpublish" : "Publish"}</button>
        <button onClick={remove} className="btn btn-ghost btn-sm">Delete</button>
      </header>

      {share.published && shareUrl && (
        <div className="px-5 py-2 text-[12px] flex items-center gap-2 border-b" style={{ borderColor: "var(--border)", background: "var(--accent-soft)" }}>
          <span>Public link:</span>
          <code className="truncate flex-1">{shareUrl}</code>
          <button className="btn btn-ghost btn-sm" onClick={() => navigator.clipboard?.writeText(shareUrl)}>Copy</button>
        </div>
      )}

      <nav className="px-5 pt-2 flex gap-1 border-b" style={{ borderColor: "var(--border)" }}>
        {(["preview", "edit", "versions"] as Tab[]).map(t => (
          <button key={t} onClick={() => setTab(t)} disabled={t === "edit" && !isText}
                  className={`px-3 py-1.5 text-[13px] rounded-t-lg ${tab === t ? "font-semibold" : ""}`}
                  style={{ borderBottom: tab === t ? "2px solid var(--accent)" : "2px solid transparent", opacity: t === "edit" && !isText ? 0.4 : 1 }}>
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </nav>

      <div className="flex-1 overflow-auto p-5">
        {tab === "preview" && <ArtifactRenderer kind={meta.kind} mimeType={meta.mime_type} content={text} blobUrl={blobUrl} title={meta.title} />}

        {tab === "edit" && isText && (
          <div className="flex flex-col gap-3 h-full">
            <textarea value={draft} onChange={e => setDraft(e.target.value)}
                      className="flex-1 min-h-[280px] w-full font-mono text-[12.5px] p-3 rounded-lg border" style={{ borderColor: "var(--border)", background: "var(--surface)" }} />
            <div className="flex gap-2">
              <button onClick={save} disabled={busy} className="btn btn-primary btn-sm">Save new version</button>
              <input value={aiInstruction} onChange={e => setAiInstruction(e.target.value)} placeholder="Ask AI to edit…"
                     className="flex-1 px-2.5 py-1.5 rounded-lg border text-[13px]" style={{ borderColor: "var(--border)", background: "var(--surface)" }} />
              <button onClick={aiEdit} disabled={busy || !aiInstruction.trim()} className="btn btn-secondary btn-sm">AI edit</button>
            </div>
          </div>
        )}

        {tab === "versions" && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1">
              {versions.map(v => (
                <div key={v.id} className="flex items-center gap-3 px-3 py-2 rounded-lg border" style={{ borderColor: "var(--border)" }}>
                  <div className="flex-1 min-w-0">
                    <div className="text-[13px] font-medium">v{v.version} {v.version === meta.version ? "(current)" : ""}</div>
                    <div className="text-[11.5px] truncate" style={{ color: "var(--text-dim)" }}>{v.edit_summary ?? ""} · {new Date(v.created_at).toLocaleString()}</div>
                  </div>
                  {v.version !== meta.version && <button onClick={() => restore(v.version)} disabled={busy} className="btn btn-ghost btn-sm">Restore</button>}
                </div>
              ))}
            </div>
            {diff && !diff.is_binary && (
              <div>
                <div className="text-[13px] font-semibold mb-2">Diff v{diff.from_version} → v{diff.to_version}</div>
                <pre className="text-[12px] overflow-auto p-3 rounded-lg font-mono" style={{ background: "var(--surface)" }}>{diff.diff || "(no changes)"}</pre>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Wire the screen into `chat/page.tsx` (one contained edit)**

Make exactly three edits to `apps/web/app/chat/page.tsx`:

1. Add the import near the top (after the existing imports, before the first component). Insert:

```tsx
import ArtifactsScreen from "../../components/artifacts/ArtifactsScreen";
```

2. Extend the `Route` type (line 21) to include `"artifacts"`:

```tsx
type Route = "chat" | "activity" | "approvals" | "memory" | "connectors" | "assistants" | "artifacts" | "settings";
```

3. Add a nav entry to the `nav` array (~line 642), after the `memory` entry:

```tsx
    { id: "artifacts"  as Route, icon: <IC.Folder size={15}/>, label: "Artifacts" },
```

4. Add the route mount in the `<main>` switch (~line 610), after the `memory` line:

```tsx
        {route === "artifacts"  && <ArtifactsScreen />}
```

Verify `routeFromPath`/`pathForRoute` (lines ~270–277) handle arbitrary routes via the generic `/${route}` form — they do (`pathForRoute` returns `/${route}` for non-chat). Confirm `routeFromPath` maps `/artifacts` to `"artifacts"`; if it has an explicit allow-list, add `"artifacts"` to it. Read those two functions and adjust only if needed.

- [ ] **Step 3: Build proof**

Run: `cd apps/web && npm run build`
Expected: build succeeds with no TypeScript errors. If `IC.Folder` is not defined, use an existing icon (`IC.Connectors` or add a `Folder` icon to the `IC` map — prefer reusing an existing one to stay surgical).

- [ ] **Step 4: Commit**

```bash
git add apps/web/components/artifacts/ArtifactsScreen.tsx apps/web/app/chat/page.tsx
git commit -m "feat(web): artifact workspace screen with versions, diff, editor, publish"
```

---

## Task 7: Artifact creation E2E proof + matrix update

**Files:**
- Test: `apps/api/tests/test_artifact_workspace.py`
- Modify: `docs/chronos_total_parity_matrix.md`

- [ ] **Step 1: Add a creation→read→download round-trip proof test**

This closes the "Artifact creation: foundation present" row (E2E create, refresh, download). Add to `apps/api/tests/test_artifact_workspace.py`:

```python
@_requires_db
@pytest.mark.asyncio
async def test_create_read_download_roundtrip_survives_refetch():
    from core.artifacts import get_artifact, read_artifact_content, save_artifact

    org = f"test-{uuid.uuid4().hex[:8]}"
    aid = await save_artifact("downloadable", kind="code", title="dl.py",
                              mime_type="text/x-python", org_id=org, created_by="member:t")
    # simulate "refresh": independent re-fetch by id
    meta = await get_artifact(aid)
    assert meta and meta["title"] == "dl.py" and meta["mime_type"] == "text/x-python"
    assert await read_artifact_content(aid) == b"downloadable"
```

- [ ] **Step 2: Run the full backend test file**

Run: `cd apps/api && python -m pytest tests/test_artifact_workspace.py -v`
Expected: all PASS (or SKIP without Postgres). Also run the governance invariants to confirm no direct-connector regressions: `python -m pytest tests/test_governance_invariants.py -v` → PASS.

- [ ] **Step 3: Update the matrix rows**

In `docs/chronos_total_parity_matrix.md`, in the `## Artifacts` table, update the **Current state** column for all five rows to reflect Phase 5 completion, citing proof. Example for the workspace row:

```
| Artifact workspace | ... | Implemented (Phase 5): full artifact browser with type filter + search, two-pane side panel, version timeline, diff viewer. Proof: apps/api/tests/test_artifact_workspace.py + web build. | ... | ... | ... |
```

Update the five rows: Artifact creation, Artifact workspace, Artifact editing, Artifact renderers, Artifact publish/share — each to "Implemented (Phase 5)" with the proof reference (`tests/test_artifact_workspace.py` and `npm --prefix apps/web run build`). Do not overstate: note Playwright E2E remains harness work per the agreed proof strategy.

- [ ] **Step 4: Commit**

```bash
git add apps/api/tests/test_artifact_workspace.py docs/chronos_total_parity_matrix.md
git commit -m "test(artifacts): creation roundtrip proof + matrix Phase 5 update"
```

---

## Self-Review (completed during planning)

- **Spec coverage:** All 5 Artifacts matrix rows mapped — creation (Task 1+7), workspace (Task 6), editing incl. AI + restore + diff (Task 2+6), renderers (Task 5), publish/share (Task 3+6). Permission seam + audit on every mutation (Task 2/3). Tenant isolation tested (Task 2). RULES 4/5 (org_id+region) on new tables (Task 1).
- **Placeholder scan:** No TBDs; every code step has full content.
- **Type consistency:** `create_version` returns head dict everywhere; `ArtifactVersion`/`Artifact` TS types match API JSON fields; renderer `classifyRenderer` shared; share endpoints return `{token, share_path}` consumed by `lib/artifacts.ts`.
- **Known deviations:** Full Playwright E2E omitted (no harness; agreed API+build proof). Binary office/notebook types get preview-or-download fallback, not editors (agreed scope bar).
```