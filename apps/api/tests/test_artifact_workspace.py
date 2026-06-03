from __future__ import annotations

import os
import socket
import uuid

import pytest


def _db_reachable() -> bool:
    host, _, port_str = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://chronos:chronos@localhost:5432/chronos"
    ).rpartition("@")[-1].partition("/")[0].rpartition(":")
    port = int(port_str) if port_str.isdigit() else 5432
    try:
        with socket.create_connection((host or "localhost", port), timeout=1):
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


@_requires_db
@pytest.mark.asyncio
async def test_edit_creates_new_version_without_clobbering():
    from core.artifacts import read_artifact_content, save_artifact
    from core.artifact_versions import create_version, read_version_content, restore_version, diff_versions

    org = f"test-{uuid.uuid4().hex[:8]}"
    aid = await save_artifact("v1 body", kind="markdown", title="E", org_id=org)
    updated = await create_version(aid, "v2 body", org_id=org, edit_summary="edit")
    assert updated["version"] == 2
    assert await read_version_content(aid, 1, org) == b"v1 body"
    assert await read_version_content(aid, 2, org) == b"v2 body"
    assert await read_artifact_content(aid) == b"v2 body"
    d = await diff_versions(aid, 1, 2, org)
    assert d["is_binary"] is False
    assert "v1 body" in d["diff"] and "v2 body" in d["diff"]
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
    assert await read_version_content(aid, 1, org_b) is None
    assert await read_version_content(aid, 1, org_a) == b"secret"


@_requires_db
@pytest.mark.asyncio
async def test_publish_then_unpublish_revokes_token():
    from core.artifacts import save_artifact
    from core.artifact_shares import create_share, get_active_share_by_token, revoke_share

    org = f"test-{uuid.uuid4().hex[:8]}"
    aid = await save_artifact("public doc", kind="markdown", title="P", org_id=org)
    share = await create_share(aid, org_id=org)
    token = share["token"]
    assert await get_active_share_by_token(token) is not None
    assert (await create_share(aid, org_id=org))["token"] == token
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


@_requires_db
@pytest.mark.asyncio
async def test_create_read_download_roundtrip_survives_refetch():
    """Artifact creation matrix row: create -> independent re-fetch (refresh) -> download bytes."""
    from core.artifacts import get_artifact, read_artifact_content, save_artifact

    org = f"test-{uuid.uuid4().hex[:8]}"
    aid = await save_artifact("downloadable", kind="code", title="dl.py",
                              mime_type="text/x-python", org_id=org, created_by="member:t")
    meta = await get_artifact(aid)
    assert meta and meta["title"] == "dl.py" and meta["mime_type"] == "text/x-python"
    assert await read_artifact_content(aid) == b"downloadable"


@_requires_db
@pytest.mark.asyncio
async def test_public_share_boundary_serves_then_revokes():
    """Publish/share matrix row: a valid token serves content; an invalid/revoked token is blocked (404)."""
    import pytest as _pytest
    from fastapi import HTTPException

    from core.artifacts import save_artifact
    from core.artifact_shares import create_share, revoke_share
    from routers.artifact_share import get_shared_content, get_shared_metadata

    org = f"test-{uuid.uuid4().hex[:8]}"
    aid = await save_artifact("shared bytes", kind="markdown", title="S", org_id=org)

    # Unknown token is blocked.
    with _pytest.raises(HTTPException) as ei:
        await get_shared_metadata("does-not-exist")
    assert ei.value.status_code == 404

    # Publish -> token serves metadata + content.
    share = await create_share(aid, org_id=org)
    token = share["token"]
    md = await get_shared_metadata(token)
    assert str(md["id"]) == aid and md["title"] == "S"
    resp = await get_shared_content(token)
    assert resp.body == b"shared bytes"

    # Unpublish -> same token now blocked (revocation actually invalidates).
    assert await revoke_share(aid, org) is True
    with _pytest.raises(HTTPException) as ei2:
        await get_shared_content(token)
    assert ei2.value.status_code == 404


def _member(org_id: str):
    """Build a Member for direct router-handler calls (auth dependency bypassed)."""
    from core.models import Member

    return Member(id=str(uuid.uuid4()), organization_id=org_id, email="t@t.io", role="user")


@_requires_db
@pytest.mark.asyncio
async def test_router_blocks_cross_org_access():
    """Tenant isolation at the route boundary: a member from another org gets 404 (not the artifact)."""
    import pytest as _pytest
    from fastapi import HTTPException

    from core.artifacts import save_artifact
    from routers.artifacts import (
        EditBody,
        edit_artifact,
        get_artifact_metadata,
    )

    org_a = f"a-{uuid.uuid4().hex[:8]}"
    org_b = f"b-{uuid.uuid4().hex[:8]}"
    aid = await save_artifact("tenant a only", kind="markdown", title="A", org_id=org_a)

    # Owner can read.
    owner_meta = await get_artifact_metadata(aid, member=_member(org_a))
    assert str(owner_meta["id"]) == aid

    # Foreign org cannot read (404, never the row).
    with _pytest.raises(HTTPException) as ei:
        await get_artifact_metadata(aid, member=_member(org_b))
    assert ei.value.status_code == 404

    # Foreign org cannot edit either.
    with _pytest.raises(HTTPException) as ei2:
        await edit_artifact(aid, EditBody(content="hijack"), member=_member(org_b))
    assert ei2.value.status_code == 404


@_requires_db
@pytest.mark.asyncio
async def test_duplicate_creates_independent_copy():
    from core.artifacts import read_artifact_content
    from routers.artifacts import duplicate_artifact

    org = f"test-{uuid.uuid4().hex[:8]}"
    from core.artifacts import save_artifact

    aid = await save_artifact("original", kind="markdown", title="Doc", org_id=org)
    copy = await duplicate_artifact(aid, member=_member(org))
    assert copy["id"] != aid
    assert copy["title"] == "Doc (copy)"
    assert copy["version"] == 1
    assert await read_artifact_content(copy["id"]) == b"original"


async def _persist_member(org_id: str):
    """Insert a real members row (project_members FKs to it) and return the Member."""
    from sqlalchemy import insert

    from core.db import engine, reflect_table
    from core.models import Member

    mid = str(uuid.uuid4())
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(
            insert(members).values(
                id=mid, organization_id=org_id, email=f"{mid[:8]}@t.io", role="owner"
            )
        )
    return Member(id=mid, organization_id=org_id, email=f"{mid[:8]}@t.io", role="owner")


async def _make_project(org_id: str, name: str = "Proj", member_id: str | None = None) -> str:
    from sqlalchemy import insert

    from core.db import engine, reflect_table

    projects = await reflect_table("projects")
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        pid = (
            await conn.execute(
                insert(projects).values(organization_id=org_id, name=name).returning(projects.c.id)
            )
        ).scalar_one()
        if member_id:
            await conn.execute(
                insert(project_members).values(
                    organization_id=org_id, project_id=pid, member_id=member_id, role="owner"
                )
            )
    return str(pid)


@_requires_db
@pytest.mark.asyncio
async def test_move_artifact_into_project_and_unlink():
    """Phase 5 `move`: an artifact can be moved into a project, appears in that
    project's artifacts, and can be unlinked back out."""
    from core.artifacts import get_artifact, save_artifact
    from routers.artifacts import MoveBody, move_artifact
    from routers.projects import get_project_artifacts

    org = f"test-{uuid.uuid4().hex[:8]}"
    member = await _persist_member(org)
    pid = await _make_project(org, member_id=member.id)
    aid = await save_artifact("movable", kind="markdown", title="M", org_id=org)

    # Not in the project before the move.
    before = await get_project_artifacts(pid, member=member)
    assert all(str(r["id"]) != aid for r in before)

    # Move -> project_id set, and it now shows up in the project.
    moved = await move_artifact(aid, MoveBody(project_id=pid), member=member)
    assert str(moved["project_id"]) == pid
    after = await get_project_artifacts(pid, member=member)
    assert any(str(r["id"]) == aid for r in after)

    # Unlink -> project_id cleared and it leaves the project.
    cleared = await move_artifact(aid, MoveBody(project_id=None), member=member)
    assert cleared["project_id"] is None
    final = await get_project_artifacts(pid, member=member)
    assert all(str(r["id"]) != aid for r in final)


@_requires_db
@pytest.mark.asyncio
async def test_move_rejects_cross_org_project_and_artifact():
    """Move is tenant-scoped: cannot target another org's project, and cannot move
    another org's artifact (both 404, never leaking the row)."""
    import pytest as _pytest
    from fastapi import HTTPException

    from core.artifacts import save_artifact
    from routers.artifacts import MoveBody, move_artifact

    org_a = f"a-{uuid.uuid4().hex[:8]}"
    org_b = f"b-{uuid.uuid4().hex[:8]}"
    aid_a = await save_artifact("a-only", kind="markdown", title="A", org_id=org_a)
    pid_b = await _make_project(org_b)

    # Org A artifact cannot be moved into Org B's project.
    with _pytest.raises(HTTPException) as ei:
        await move_artifact(aid_a, MoveBody(project_id=pid_b), member=_member(org_a))
    assert ei.value.status_code == 404

    # Org B member cannot move Org A's artifact at all.
    pid_a = await _make_project(org_a)
    with _pytest.raises(HTTPException) as ei2:
        await move_artifact(aid_a, MoveBody(project_id=pid_a), member=_member(org_b))
    assert ei2.value.status_code == 404
