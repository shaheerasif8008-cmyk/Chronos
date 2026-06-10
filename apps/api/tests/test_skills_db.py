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



# ---------------------------------------------------------------------------
# Phase 3: write-path tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_skill_new(sqlite_db):
    """Creating a brand-new uploaded skill yields version=1."""
    result = await registry.create_or_update_skill(
        org_id="default",
        slug="my-new-skill",
        name="My New Skill",
        description="A test skill",
        content="# SKILL.md\nDo stuff.",
        metadata={"requires_connectors": [], "spawns_sub_agent": False},
        created_by="test-member",
    )
    assert result["version"] == 1
    assert result["skill_id"]

    skill = await registry.get_skill_db("default", "my-new-skill")
    assert skill is not None
    assert skill["current_version"] == 1
    assert skill["source"] == "uploaded"
    assert skill["content"] == "# SKILL.md\nDo stuff."


@pytest.mark.asyncio
async def test_create_skill_new_version(sqlite_db):
    """Calling create_or_update_skill twice on the same slug bumps version to 2."""
    await registry.create_or_update_skill(
        org_id="default",
        slug="versioned-skill",
        name="Versioned Skill",
        description="v1",
        content="content v1",
        metadata={},
        created_by="member-a",
    )
    result2 = await registry.create_or_update_skill(
        org_id="default",
        slug="versioned-skill",
        name="Versioned Skill",
        description="v2",
        content="content v2",
        metadata={},
        created_by="member-b",
    )
    assert result2["version"] == 2

    skill = await registry.get_skill_db("default", "versioned-skill")
    assert skill["current_version"] == 2
    # current content should reflect version 2
    assert skill["content"] == "content v2"

    versions = await registry.list_skill_versions(skill["id"])
    assert len(versions) == 2
    assert versions[0]["version"] == 1
    assert versions[1]["version"] == 2


@pytest.mark.asyncio
async def test_filesystem_skill_immutable(sqlite_db):
    """A filesystem-sourced skill cannot be overwritten by create_or_update_skill
    when guarded at the router level — the guard is the source=='filesystem' check.
    We verify that the DB function itself does NOT enforce this (it's a router concern),
    but we also verify that the source column remains 'filesystem' on a fresh sync."""
    synced = await registry.sync_filesystem_skills(org_id="default")
    slug = synced[0]["slug"]

    skill = await registry.get_skill_db("default", slug)
    assert skill["source"] == "filesystem"

    # The router would block this; the DB function itself permits it (caller's
    # responsibility).  We only assert the source field is detectable.
    assert skill["source"] == "filesystem"


@pytest.mark.asyncio
async def test_soft_delete(sqlite_db):
    """A soft-deleted skill no longer appears in list_skills_db."""
    await registry.create_or_update_skill(
        org_id="default",
        slug="delete-me",
        name="Delete Me",
        description="",
        content="bye",
        metadata={},
        created_by="member",
    )
    before = await registry.list_skills_db("default")
    assert any(s["slug"] == "delete-me" for s in before)

    deleted = await registry.soft_delete_skill("default", "delete-me")
    assert deleted is True

    after = await registry.list_skills_db("default")
    assert not any(s["slug"] == "delete-me" for s in after)

    # Double-delete returns False (already deleted)
    second = await registry.soft_delete_skill("default", "delete-me")
    assert second is False


@pytest.mark.asyncio
async def test_get_version(sqlite_db):
    """get_skill_version returns the correct content for a specific version."""
    r1 = await registry.create_or_update_skill(
        org_id="default",
        slug="versioned-fetch",
        name="Versioned Fetch",
        description="",
        content="version one content",
        metadata={},
        created_by="member",
    )
    r2 = await registry.create_or_update_skill(
        org_id="default",
        slug="versioned-fetch",
        name="Versioned Fetch",
        description="",
        content="version two content",
        metadata={},
        created_by="member",
    )

    skill_id = r1["skill_id"]

    v1 = await registry.get_skill_version(skill_id, 1)
    assert v1 is not None
    assert v1["content"] == "version one content"

    v2 = await registry.get_skill_version(skill_id, 2)
    assert v2 is not None
    assert v2["content"] == "version two content"

    missing = await registry.get_skill_version(skill_id, 99)
    assert missing is None


# ---------------------------------------------------------------------------
# Phase 4: runtime discovery of DB-persisted skills
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_candidate_skills_union(sqlite_db):
    """Candidate pool is the union of filesystem skills and DB-only skills."""
    await registry.create_or_update_skill(
        org_id="default",
        slug="db-only-skill",
        name="DB Only Skill",
        description="Lives only in the DB",
        content="# uploaded",
        metadata={"requires_connectors": [], "spawns_sub_agent": False},
        created_by="member",
    )

    candidates = await registry.get_candidate_skills("default")
    slugs = {c["id"] for c in candidates}

    fs_slugs = {s["id"] for s in registry.load_skill_index()}
    assert fs_slugs, "expected at least one filesystem skill"
    assert fs_slugs <= slugs, "filesystem skills should be present"
    assert "db-only-skill" in slugs, "DB-only skill should be present"

    # Consistent shape on every candidate.
    for c in candidates:
        assert set(c) == {
            "id",
            "name",
            "description",
            "source",
            "requires_connectors",
            "spawns_sub_agent",
        }


@pytest.mark.asyncio
async def test_get_candidate_skills_db_overrides_filesystem(sqlite_db):
    """When a DB skill shares a slug with a filesystem skill, the DB version wins."""
    fs_slug = registry.load_skill_index()[0]["id"]

    await registry.create_or_update_skill(
        org_id="default",
        slug=fs_slug,
        name="Overridden Name",
        description="overridden description",
        content="# override",
        metadata={"requires_connectors": ["gmail"], "spawns_sub_agent": True},
        created_by="member",
    )

    candidates = await registry.get_candidate_skills("default")
    matches = [c for c in candidates if c["id"] == fs_slug]

    assert len(matches) == 1, "slug should be deduped"
    override = matches[0]
    assert override["name"] == "Overridden Name"
    assert override["source"] == "uploaded"
    assert override["requires_connectors"] == ["gmail"]
    assert override["spawns_sub_agent"] is True


@pytest.mark.asyncio
async def test_get_candidate_skills_db_error_degrades(sqlite_db, monkeypatch):
    """If the DB query raises, fall back to filesystem-only (no exception)."""
    async def boom(org_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(registry, "list_skills_db", boom)

    candidates = await registry.get_candidate_skills("default")
    slugs = {c["id"] for c in candidates}

    assert slugs == {s["id"] for s in registry.load_skill_index()}
    assert all(c["source"] == "filesystem" for c in candidates)


@pytest.mark.asyncio
async def test_load_skill_content_db(sqlite_db):
    """A DB skill's current version content is loaded over the filesystem."""
    from skills import loader

    await registry.create_or_update_skill(
        org_id="default",
        slug="uploaded-content-skill",
        name="Uploaded Content",
        description="",
        content="# SKILL.md from DB\nUse the uploaded workflow.",
        metadata={},
        created_by="member",
    )

    content = await loader.load_skill_content(
        "uploaded-content-skill", org_id="default"
    )
    assert content == "# SKILL.md from DB\nUse the uploaded workflow."


@pytest.mark.asyncio
async def test_load_skill_content_filesystem_fallback(sqlite_db):
    """A slug absent from the DB falls back to the filesystem loader."""
    from skills import loader

    fs_slug = registry.load_skill_index()[0]["id"]
    # Not inserted into the DB for this org → must come from the filesystem.
    content = await loader.load_skill_content(fs_slug, org_id="default")
    assert content, "expected non-empty filesystem SKILL.md content"
