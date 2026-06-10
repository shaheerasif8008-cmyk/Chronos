"""Tests for Phase 2 skills DB persistence + versioning (skills.registry).

No live Postgres is required: an in-memory SQLite engine is substituted for the
shared async engine, and SQLite-friendly mirrors of the ``skills`` /
``skill_versions`` tables are injected into the reflect_table cache. The
registry code under test only relies on generic SQLAlchemy Core constructs, so
it runs unchanged against SQLite.

Covered:
1. sync_filesystem_skills is idempotent (run twice -> no duplicate rows).
2. list_skills_db only returns the requester's org.
3. a skill_versions row (version 1) is created on sync.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import (
    JSON,
    TIMESTAMP,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.ext.asyncio import create_async_engine

import core.db as core_db
from skills import registry


# SQLite-friendly mirror of migration 0032 (JSON stands in for ARRAY/JSONB).
_md = MetaData()

skills_table = Table(
    "skills",
    _md,
    Column("id", Text, primary_key=True),
    Column("organization_id", Text, nullable=False, server_default="default"),
    Column("region", Text, nullable=False, server_default="us"),
    Column("slug", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("description", Text),
    Column("requires_connectors", JSON, nullable=False, server_default="[]"),
    Column("spawns_sub_agent", Boolean, nullable=False, server_default="0"),
    Column("source", Text, nullable=False, server_default="filesystem"),
    Column("current_version", Integer, nullable=False, server_default="1"),
    Column("is_deleted", Boolean, nullable=False, server_default="0"),
    Column("created_at", TIMESTAMP(timezone=True), server_default=func.now()),
    Column("updated_at", TIMESTAMP(timezone=True), server_default=func.now()),
    UniqueConstraint("organization_id", "slug", name="uq_skills_org_slug"),
)

skill_versions_table = Table(
    "skill_versions",
    _md,
    Column("id", Text, primary_key=True),
    Column("organization_id", Text, nullable=False, server_default="default"),
    Column("region", Text, nullable=False, server_default="us"),
    Column("skill_id", Text, ForeignKey("skills.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("content", Text),
    Column("metadata", JSON, nullable=False, server_default="{}"),
    Column("created_at", TIMESTAMP(timezone=True), server_default=func.now()),
    Column("created_by", Text),
    UniqueConstraint("skill_id", "version", name="uq_skill_versions_skill_version"),
)


@pytest_asyncio.fixture
async def sqlite_db(monkeypatch):
    """Point core.db at an in-memory SQLite engine with the skills tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(_md.create_all)

    monkeypatch.setattr(core_db, "engine", engine)
    # Pre-seed reflect_table's cache so registry uses our SQLite tables.
    monkeypatch.setattr(core_db, "_TABLE_CACHE", {
        "skills": skills_table,
        "skill_versions": skill_versions_table,
    })
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sync_is_idempotent(sqlite_db):
    first = await registry.sync_filesystem_skills(org_id="default")
    assert first, "expected at least one filesystem skill to sync"
    assert all(r["created"] for r in first)

    second = await registry.sync_filesystem_skills(org_id="default")
    assert all(not r["created"] for r in second)

    async with sqlite_db.connect() as conn:
        skill_count = (await conn.execute(select(func.count()).select_from(skills_table))).scalar()
        version_count = (await conn.execute(select(func.count()).select_from(skill_versions_table))).scalar()

    assert skill_count == len(first)
    # One version row per skill; idempotent re-run adds none.
    assert version_count == len(first)


@pytest.mark.asyncio
async def test_list_skills_db_is_tenant_scoped(sqlite_db):
    await registry.sync_filesystem_skills(org_id="default")
    await registry.sync_filesystem_skills(org_id="other-org")

    default_skills = await registry.list_skills_db("default")
    other_skills = await registry.list_skills_db("other-org")

    assert default_skills, "default org should have skills"
    assert all(s["organization_id"] == "default" for s in default_skills)
    assert all(s["organization_id"] == "other-org" for s in other_skills)


@pytest.mark.asyncio
async def test_sync_creates_version_one(sqlite_db):
    synced = await registry.sync_filesystem_skills(org_id="default")
    slug = synced[0]["slug"]

    skill = await registry.get_skill_db("default", slug)
    assert skill is not None
    assert skill["current_version"] == 1

    async with sqlite_db.connect() as conn:
        rows = (
            await conn.execute(
                select(skill_versions_table).where(skill_versions_table.c.version == 1)
            )
        ).mappings().all()
    assert any(r["skill_id"] == skill["id"] for r in rows)
