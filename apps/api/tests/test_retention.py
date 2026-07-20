"""Production retention proof: aging, legal holds, object cleanup, and retries."""

from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Text, cast, delete, insert, select


def _db_reachable() -> bool:
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://chronos:chronos@localhost:55432/chronos",
    )
    authority = url.rpartition("@")[2].partition("/")[0]
    host, _, port_text = authority.rpartition(":")
    try:
        with socket.create_connection(
            (host or "localhost", int(port_text or "5432")), timeout=1
        ):
            return True
    except OSError:
        return False


_requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable")


async def _insert_policy(org_id: str) -> None:
    from core.db import engine, reflect_table

    documents = await reflect_table("settings_documents")
    async with engine.begin() as conn:
        await conn.execute(
            insert(documents).values(
                organization_id=org_id,
                region="us",
                scope="org",
                scope_id=org_id,
                section="memory",
                values={
                    "retention_enabled": True,
                    "retention_days": 30,
                    "deleted_retention_days": 7,
                    "deleted_artifact_retention_days": 7,
                },
                updated_by="retention-test",
            )
        )


async def _insert_memory(
    org_id: str,
    memory_id: str,
    *,
    now: datetime,
    days_old: int,
    deleted: bool = False,
    pinned: bool = False,
) -> None:
    from core.db import engine, reflect_table

    memory = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        await conn.execute(
            insert(memory).values(
                id=memory_id,
                organization_id=org_id,
                region="us",
                scope="org",
                scope_id=org_id,
                content=f"retention fixture {memory_id}",
                source="explicit",
                is_deleted=deleted,
                is_pinned=pinned,
                created_at=now - timedelta(days=days_old),
                updated_at=now - timedelta(days=days_old),
            )
        )


async def _insert_artifact(
    org_id: str,
    artifact_id: uuid.UUID,
    *,
    now: datetime,
    days_old: int,
    deleted: bool = True,
) -> Path:
    from core.db import engine, reflect_table

    artifacts = await reflect_table("artifacts")
    versions = await reflect_table("artifact_versions")
    object_path = f"local://{artifact_id}/v1"
    local_path = Path("/tmp/chronos_artifacts") / str(artifact_id) / "v1"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(b"retention-object")
    async with engine.begin() as conn:
        await conn.execute(
            insert(artifacts).values(
                id=artifact_id,
                organization_id=org_id,
                region="us",
                kind="file",
                title="aged deletion fixture",
                object_path=object_path,
                version=1,
                is_deleted=deleted,
                created_at=now - timedelta(days=days_old),
                updated_at=now - timedelta(days=days_old),
            )
        )
        await conn.execute(
            insert(versions).values(
                organization_id=org_id,
                region="us",
                artifact_id=artifact_id,
                version=1,
                object_path=object_path,
                edit_summary="initial",
                created_at=now - timedelta(days=days_old),
            )
        )
    return local_path


async def _cleanup(org_ids: list[str]) -> None:
    """Remove mutable fixture rows. Append-only audit evidence stays by design."""

    from core.db import engine, reflect_table

    table_names = [
        "retention_holds",
        "artifact_shares",
        "artifact_versions",
        "artifacts",
        "memory_usage_log",
        "memory_entries",
        "settings_documents",
    ]
    tables = [await reflect_table(name) for name in table_names]
    async with engine.begin() as conn:
        for table in tables:
            await conn.execute(
                delete(table).where(table.c.organization_id.in_(org_ids))
            )


@_requires_db
@pytest.mark.asyncio
async def test_retention_dry_run_then_execute_respects_holds_and_tenants():
    from core import retention
    from core.db import engine, reflect_table

    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    org_id = f"retention-{uuid.uuid4().hex[:10]}"
    other_org = f"retention-other-{uuid.uuid4().hex[:10]}"
    active_id = str(uuid.uuid4())
    deleted_id = str(uuid.uuid4())
    pinned_id = str(uuid.uuid4())
    held_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    artifact_id = uuid.uuid4()
    object_file: Path | None = None

    try:
        await _insert_policy(org_id)
        await _insert_memory(org_id, active_id, now=now, days_old=40)
        await _insert_memory(
            org_id, deleted_id, now=now, days_old=10, deleted=True
        )
        await _insert_memory(
            org_id, pinned_id, now=now, days_old=40, pinned=True
        )
        await _insert_memory(org_id, held_id, now=now, days_old=40)
        await _insert_memory(other_org, other_id, now=now, days_old=40)
        object_file = await _insert_artifact(
            org_id, artifact_id, now=now, days_old=10
        )

        holds = await reflect_table("retention_holds")
        async with engine.begin() as conn:
            await conn.execute(
                insert(holds).values(
                    organization_id=org_id,
                    region="us",
                    resource_type="memory",
                    resource_id=held_id,
                    reason="Client litigation hold",
                    created_by="legal-admin",
                )
            )

        dry_run = await retention.run_retention(
            org_id, dry_run=True, actor_id="admin-1", now=now
        )
        assert dry_run["memory_soft_delete_candidates"] == 1
        assert dry_run["memory_hard_delete_candidates"] == 1
        assert dry_run["artifact_purge_candidates"] == 1
        assert dry_run["artifact_object_delete_candidates"] == 1
        assert dry_run["pinned_memories_excluded"] == 1
        assert dry_run["held_resources_excluded"] == 1
        assert object_file.exists()

        memory = await reflect_table("memory_entries")
        artifacts = await reflect_table("artifacts")
        async with engine.begin() as conn:
            active_before = (
                await conn.execute(
                    select(memory.c.is_deleted).where(memory.c.id == active_id)
                )
            ).scalar_one()
            artifact_before = (
                await conn.execute(
                    select(artifacts.c.id).where(
                        cast(artifacts.c.id, Text) == str(artifact_id)
                    )
                )
            ).first()
        assert active_before is False
        assert artifact_before is not None

        executed = await retention.run_retention(
            org_id, dry_run=False, actor_id="admin-1", now=now
        )
        assert executed["memory_soft_deleted"] == 1
        assert executed["memory_hard_deleted"] == 1
        assert executed["artifact_objects_deleted"] == 1
        assert executed["artifact_metadata_deleted"] == 1
        assert executed["artifact_purge_failures"] == 0
        assert not object_file.exists()

        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(memory.c.id, memory.c.is_deleted).where(
                        memory.c.id.in_([active_id, deleted_id, pinned_id, held_id, other_id])
                    )
                )
            ).all()
            artifact_after = (
                await conn.execute(
                    select(artifacts.c.id).where(
                        cast(artifacts.c.id, Text) == str(artifact_id)
                    )
                )
            ).first()
        state = {str(row[0]): bool(row[1]) for row in rows}
        assert state[active_id] is True
        assert deleted_id not in state
        assert state[pinned_id] is False
        assert state[held_id] is False
        assert state[other_id] is False
        assert artifact_after is None
    finally:
        await _cleanup([org_id, other_org])
        if object_file is not None:
            object_file.unlink(missing_ok=True)
            try:
                object_file.parent.rmdir()
            except OSError:
                pass


@_requires_db
@pytest.mark.asyncio
async def test_artifact_object_failure_preserves_metadata_for_retry(monkeypatch):
    from core import retention
    from core.db import engine, reflect_table

    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    org_id = f"retention-failure-{uuid.uuid4().hex[:10]}"
    artifact_id = uuid.uuid4()
    object_file: Path | None = None

    async def fail_delete(_org_id: str, _object_path: str) -> None:
        raise RuntimeError("fixture object store outage")

    try:
        await _insert_policy(org_id)
        object_file = await _insert_artifact(
            org_id, artifact_id, now=now, days_old=10
        )
        monkeypatch.setattr(retention, "_delete_artifact_object", fail_delete)

        result = await retention.run_retention(
            org_id, dry_run=False, actor_id="admin-1", now=now
        )
        assert result["artifact_purge_candidates"] == 1
        assert result["artifact_purge_failures"] == 1
        assert result["artifact_metadata_deleted"] == 0
        assert result["failed_artifact_ids"] == [str(artifact_id)]

        artifacts = await reflect_table("artifacts")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(artifacts.c.is_deleted).where(
                        artifacts.c.organization_id == org_id,
                        cast(artifacts.c.id, Text) == str(artifact_id),
                    )
                )
            ).first()
        assert row is not None and row[0] is True
        assert object_file.exists()
    finally:
        await _cleanup([org_id])
        if object_file is not None:
            object_file.unlink(missing_ok=True)
            try:
                object_file.parent.rmdir()
            except OSError:
                pass


@_requires_db
@pytest.mark.asyncio
async def test_legal_hold_create_is_idempotent_and_release_is_audited():
    from core import retention

    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    org_id = f"retention-hold-{uuid.uuid4().hex[:10]}"
    other_org = f"retention-hold-other-{uuid.uuid4().hex[:10]}"
    memory_id = str(uuid.uuid4())

    try:
        await _insert_memory(org_id, memory_id, now=now, days_old=1)
        assert await retention.resource_exists(org_id, "memory", memory_id) is True
        assert await retention.resource_exists(other_org, "memory", memory_id) is False

        first = await retention.create_hold(
            org_id=org_id,
            region="us",
            resource_type="memory",
            resource_id=memory_id,
            reason="Preserve for client legal review",
            actor_id="admin-1",
        )
        duplicate = await retention.create_hold(
            org_id=org_id,
            region="us",
            resource_type="memory",
            resource_id=memory_id,
            reason="A concurrent duplicate request",
            actor_id="admin-1",
        )
        assert str(first["id"]) == str(duplicate["id"])
        assert len(await retention.list_holds(org_id)) == 1

        assert await retention.release_hold(org_id, str(first["id"]), "admin-2") is True
        assert await retention.release_hold(org_id, str(first["id"]), "admin-2") is False
        assert await retention.list_holds(org_id) == []
        history = await retention.list_holds(org_id, active_only=False)
        assert len(history) == 1
        assert history[0]["released_by"] == "admin-2"
        assert history[0]["released_at"] is not None
    finally:
        await _cleanup([org_id, other_org])


@_requires_db
@pytest.mark.asyncio
async def test_manual_delete_paths_cannot_bypass_retention_holds():
    from core import retention
    from core.db import engine, reflect_table
    from core.models import Member

    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    org_id = f"retention-manual-{uuid.uuid4().hex[:10]}"
    eligible_id = str(uuid.uuid4())
    held_id = str(uuid.uuid4())
    pinned_id = str(uuid.uuid4())
    artifact_id = uuid.uuid4()
    object_file: Path | None = None

    try:
        await _insert_memory(org_id, eligible_id, now=now, days_old=1)
        await _insert_memory(org_id, held_id, now=now, days_old=1)
        await _insert_memory(
            org_id, pinned_id, now=now, days_old=1, pinned=True
        )
        object_file = await _insert_artifact(
            org_id, artifact_id, now=now, days_old=1, deleted=False
        )
        for resource_type, resource_id in (
            ("memory", held_id),
            ("artifact", str(artifact_id)),
        ):
            await retention.create_hold(
                org_id=org_id,
                region="us",
                resource_type=resource_type,
                resource_id=resource_id,
                reason="Preserve during client legal review",
                actor_id="admin-1",
            )

        member = Member(
            id="admin-1",
            organization_id=org_id,
            email="admin@example.com",
            role="admin",
        )
        with pytest.raises(retention.RetentionResourceHeld):
            await retention.soft_delete_memory_if_allowed(held_id, member)
        with pytest.raises(retention.RetentionResourceHeld):
            await retention.soft_delete_artifact_if_allowed(
                org_id, str(artifact_id)
            )

        bulk = await retention.soft_delete_all_memory(org_id, actor_id="admin-1")
        assert bulk == {
            "deleted": 1,
            "pinned_excluded": 1,
            "held_excluded": 1,
            "organization_hold": False,
        }

        memory = await reflect_table("memory_entries")
        artifacts = await reflect_table("artifacts")
        async with engine.begin() as conn:
            memory_rows = (
                await conn.execute(
                    select(memory.c.id, memory.c.is_deleted).where(
                        memory.c.id.in_([eligible_id, held_id, pinned_id])
                    )
                )
            ).all()
            artifact_deleted = (
                await conn.execute(
                    select(artifacts.c.is_deleted).where(
                        artifacts.c.organization_id == org_id,
                        cast(artifacts.c.id, Text) == str(artifact_id),
                    )
                )
            ).scalar_one()
        state = {str(row[0]): bool(row[1]) for row in memory_rows}
        assert state == {eligible_id: True, held_id: False, pinned_id: False}
        assert artifact_deleted is False
        assert object_file.exists()
    finally:
        await _cleanup([org_id])
        if object_file is not None:
            object_file.unlink(missing_ok=True)
            try:
                object_file.parent.rmdir()
            except OSError:
                pass


@_requires_db
@pytest.mark.asyncio
async def test_stale_runtime_document_cannot_reintroduce_fake_controls():
    from core.db import engine, reflect_table
    from core.models import Member
    from core.settings_store import get_settings_doc

    org_id = f"runtime-settings-{uuid.uuid4().hex[:10]}"
    documents = await reflect_table("settings_documents")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                insert(documents).values(
                    organization_id=org_id,
                    region="us",
                    scope="org",
                    scope_id=org_id,
                    section="runtime",
                    values={
                        "runtime_mode": "managed",
                        "restart_policy": "always",
                        "max_task_queue_size": 25,
                    },
                )
            )
        doc = await get_settings_doc(
            Member(
                id="admin-1",
                organization_id=org_id,
                email="admin@example.com",
                role="admin",
            ),
            "runtime",
        )
        assert doc["max_task_queue_size"] == 25
        assert "runtime_mode" not in doc
        assert "restart_policy" not in doc
    finally:
        await _cleanup([org_id])


def test_settings_reject_controls_without_runtime_consumers():
    from fastapi import HTTPException
    from routers.settings import _validate_section

    with pytest.raises(HTTPException) as exc:
        _validate_section("runtime", {"restart_policy": "always"})
    assert exc.value.status_code == 400
    assert "Unsupported runtime settings" in str(exc.value.detail)

    with pytest.raises(HTTPException) as employee_exc:
        _validate_section("ai_employee", {"runtime_auto_start": False})
    assert employee_exc.value.status_code == 400
    assert "Unsupported AI employee settings" in str(employee_exc.value.detail)


def test_retention_days_are_bounded():
    from fastapi import HTTPException
    from routers.settings import _validate_section

    with pytest.raises(HTTPException) as exc:
        _validate_section("memory", {"retention_days": 0})
    assert exc.value.status_code == 400
    assert "between 1 and 3650" in str(exc.value.detail)
