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
