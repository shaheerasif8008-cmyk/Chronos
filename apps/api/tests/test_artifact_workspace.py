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
