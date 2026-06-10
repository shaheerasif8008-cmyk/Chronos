from __future__ import annotations

import json
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = ROOT / "skills"


@lru_cache
def load_skill_index() -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    if not SKILLS_ROOT.exists():
        return index
    for meta_path in sorted(SKILLS_ROOT.glob("*/metadata.json")):
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            continue
        skill_id = str(meta.get("id") or meta_path.parent.name)
        index.append(
            {
                "id": skill_id,
                "name": str(meta.get("name") or skill_id),
                "description": str(meta.get("description") or ""),
                "path": str(meta_path.parent),
                "requires_connectors": list(meta.get("requires_connectors") or []),
                "spawns_sub_agent": bool(meta.get("spawns_sub_agent", False)),
            }
        )
    return index


def _read_skill_md(skill_path: str) -> str:
    md = Path(skill_path) / "SKILL.md"
    if md.exists():
        return md.read_text()
    return ""


async def sync_filesystem_skills(org_id: str = "default") -> list[dict[str, Any]]:
    """Upsert each filesystem skill into the ``skills`` table for ``org_id``.

    Idempotent: re-running does not create duplicate skill or version rows. Each
    new skill gets a ``skill_versions`` row at version 1 carrying the SKILL.md
    content and parsed metadata. Existing skills are left untouched (filesystem
    remains the seed source; later phases handle re-versioning on change).
    """
    from sqlalchemy import insert, select
    from sqlalchemy.sql import func

    from core.config import settings
    from core.db import engine, reflect_table

    skills_t = await reflect_table("skills")
    versions_t = await reflect_table("skill_versions")

    synced: list[dict[str, Any]] = []
    async with engine.begin() as conn:
        for skill in load_skill_index():
            existing = (
                await conn.execute(
                    select(skills_t.c.id, skills_t.c.current_version).where(
                        skills_t.c.organization_id == org_id,
                        skills_t.c.slug == skill["id"],
                    )
                )
            ).first()

            if existing is not None:
                synced.append({"slug": skill["id"], "id": str(existing.id), "created": False})
                continue

            skill_id = str(uuid.uuid4())
            await conn.execute(
                insert(skills_t).values(
                    id=skill_id,
                    organization_id=org_id,
                    region=settings.region,
                    slug=skill["id"],
                    name=skill["name"],
                    description=skill["description"],
                    requires_connectors=list(skill["requires_connectors"]),
                    spawns_sub_agent=bool(skill["spawns_sub_agent"]),
                    source="filesystem",
                    current_version=1,
                )
            )
            await conn.execute(
                insert(versions_t).values(
                    id=str(uuid.uuid4()),
                    organization_id=org_id,
                    region=settings.region,
                    skill_id=skill_id,
                    version=1,
                    content=_read_skill_md(skill["path"]),
                    metadata={
                        "id": skill["id"],
                        "name": skill["name"],
                        "description": skill["description"],
                        "requires_connectors": list(skill["requires_connectors"]),
                        "spawns_sub_agent": bool(skill["spawns_sub_agent"]),
                    },
                    created_by="chronos",
                )
            )
            synced.append({"slug": skill["id"], "id": skill_id, "created": True})
    return synced


async def create_or_update_skill(
    org_id: str,
    slug: str,
    name: str,
    description: str,
    content: str,
    metadata: dict,
    created_by: str,
    source: str = "uploaded",
) -> dict[str, Any]:
    """Upsert a skill row and insert a new skill_versions row.

    Returns {"skill_id": ..., "version": ...}.
    """
    from sqlalchemy import insert, select, update
    from sqlalchemy.sql import func

    from core.config import settings
    from core.db import engine, reflect_table

    skills_t = await reflect_table("skills")
    versions_t = await reflect_table("skill_versions")

    requires_connectors = list(metadata.get("requires_connectors") or [])
    spawns_sub_agent = bool(metadata.get("spawns_sub_agent", False))

    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                select(skills_t.c.id, skills_t.c.current_version).where(
                    skills_t.c.organization_id == org_id,
                    skills_t.c.slug == slug,
                )
            )
        ).first()

        if existing is not None:
            skill_id = str(existing.id)
            new_version = existing.current_version + 1
            await conn.execute(
                update(skills_t)
                .where(skills_t.c.id == skill_id)
                .values(
                    name=name,
                    description=description,
                    requires_connectors=requires_connectors,
                    spawns_sub_agent=spawns_sub_agent,
                    current_version=new_version,
                    updated_at=func.now(),
                )
            )
        else:
            skill_id = str(uuid.uuid4())
            new_version = 1
            await conn.execute(
                insert(skills_t).values(
                    id=skill_id,
                    organization_id=org_id,
                    region=settings.region,
                    slug=slug,
                    name=name,
                    description=description,
                    requires_connectors=requires_connectors,
                    spawns_sub_agent=spawns_sub_agent,
                    source=source,
                    current_version=new_version,
                )
            )

        await conn.execute(
            insert(versions_t).values(
                id=str(uuid.uuid4()),
                organization_id=org_id,
                region=settings.region,
                skill_id=skill_id,
                version=new_version,
                content=content,
                metadata=metadata,
                created_by=created_by,
            )
        )

    return {"skill_id": skill_id, "version": new_version}


async def get_skill_version(skill_id: str, version: int) -> dict[str, Any] | None:
    """Fetch a specific version row for a skill."""
    from sqlalchemy import select

    from core.db import engine, reflect_table

    versions_t = await reflect_table("skill_versions")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(versions_t).where(
                    versions_t.c.skill_id == skill_id,
                    versions_t.c.version == version,
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def list_skill_versions(skill_id: str) -> list[dict[str, Any]]:
    """Return version history (no content) for a skill, oldest first."""
    from sqlalchemy import select

    from core.db import engine, reflect_table

    versions_t = await reflect_table("skill_versions")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    versions_t.c.version,
                    versions_t.c.created_at,
                    versions_t.c.created_by,
                )
                .where(versions_t.c.skill_id == skill_id)
                .order_by(versions_t.c.version)
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def soft_delete_skill(org_id: str, slug: str) -> bool:
    """Set is_deleted=True for a skill. Returns True if the skill was found."""
    from sqlalchemy import select, update
    from sqlalchemy.sql import func

    from core.db import engine, reflect_table

    skills_t = await reflect_table("skills")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(skills_t.c.id).where(
                    skills_t.c.organization_id == org_id,
                    skills_t.c.slug == slug,
                    skills_t.c.is_deleted.is_(False),
                )
            )
        ).first()
        if row is None:
            return False
        await conn.execute(
            update(skills_t)
            .where(skills_t.c.id == row.id)
            .values(is_deleted=True, updated_at=func.now())
        )
    return True


async def list_skills_db(org_id: str) -> list[dict[str, Any]]:
    """Return DB-persisted, non-deleted skills scoped to ``org_id``."""
    from sqlalchemy import select

    from core.db import engine, reflect_table

    skills_t = await reflect_table("skills")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(skills_t)
                .where(
                    skills_t.c.organization_id == org_id,
                    skills_t.c.is_deleted.is_(False),
                )
                .order_by(skills_t.c.slug)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def get_skill_db(org_id: str, slug: str) -> dict[str, Any] | None:
    """Return one DB skill plus its current version content, scoped to ``org_id``."""
    from sqlalchemy import select

    from core.db import engine, reflect_table

    skills_t = await reflect_table("skills")
    versions_t = await reflect_table("skill_versions")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(skills_t).where(
                    skills_t.c.organization_id == org_id,
                    skills_t.c.slug == slug,
                    skills_t.c.is_deleted.is_(False),
                )
            )
        ).mappings().first()
        if row is None:
            return None
        skill = dict(row)
        version = (
            await conn.execute(
                select(versions_t.c.content, versions_t.c.metadata).where(
                    versions_t.c.organization_id == org_id,
                    versions_t.c.skill_id == skill["id"],
                    versions_t.c.version == skill["current_version"],
                )
            )
        ).mappings().first()
    skill["content"] = version["content"] if version else None
    skill["version_metadata"] = dict(version["metadata"]) if version else None
    return skill
