from __future__ import annotations

import json
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def _runtime_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        skills_dir = parent / "skills"
        if skills_dir.is_dir() and (
            any(skills_dir.glob("*/SKILL.md")) or any(skills_dir.glob("*/metadata.json"))
        ):
            return parent
        if (parent / "apps" / "api").exists():
            return parent
    return current.parents[1]


ROOT = _runtime_root()
SKILLS_ROOT = ROOT / "skills"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_skill_frontmatter(content: str) -> dict[str, Any]:
    """Parse the YAML frontmatter from a SKILL.md body (Claude's canonical format).

    Returns the parsed mapping, or an empty dict when no valid frontmatter exists.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_skill_metadata(skill_dir: Path) -> dict[str, Any] | None:
    """Resolve a skill's Level-1 metadata.

    Precedence mirrors Claude: the SKILL.md YAML frontmatter (``name``,
    ``description``) is canonical. metadata.json supplements it for Chronos-only
    fields (requires_connectors, spawns_sub_agent) and provides backward-compat
    for skills authored before the frontmatter convention.
    """
    skill_md = skill_dir / "SKILL.md"
    meta_json = skill_dir / "metadata.json"
    if not skill_md.exists() and not meta_json.exists():
        return None

    frontmatter: dict[str, Any] = {}
    if skill_md.exists():
        try:
            frontmatter = parse_skill_frontmatter(skill_md.read_text())
        except OSError:
            frontmatter = {}

    legacy: dict[str, Any] = {}
    if meta_json.exists():
        try:
            legacy = json.loads(meta_json.read_text())
        except json.JSONDecodeError:
            legacy = {}

    skill_id = str(frontmatter.get("name") or legacy.get("id") or skill_dir.name)
    name = str(frontmatter.get("name") or legacy.get("name") or skill_dir.name)
    description = str(frontmatter.get("description") or legacy.get("description") or "")
    return {
        "id": skill_id,
        "name": name,
        "description": description,
        "path": str(skill_dir),
        "requires_connectors": list(
            frontmatter.get("requires_connectors") or legacy.get("requires_connectors") or []
        ),
        "spawns_sub_agent": bool(
            frontmatter.get("spawns_sub_agent", legacy.get("spawns_sub_agent", False))
        ),
    }


@lru_cache
def load_skill_index() -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    if not SKILLS_ROOT.exists():
        return index
    for skill_dir in sorted(p for p in SKILLS_ROOT.iterdir() if p.is_dir()):
        meta = _read_skill_metadata(skill_dir)
        if meta is not None:
            index.append(meta)
    return index


async def get_candidate_skills(org_id: str) -> list[dict[str, Any]]:
    """Return the union of filesystem skills and DB skills for ``org_id``.

    Each candidate has a consistent shape:
        {id, name, description, source, requires_connectors, spawns_sub_agent}

    Dedupe is by slug, with the DB version preferred (an uploaded override may
    be newer than the filesystem seed). If the DB query fails for any reason
    (DB unavailable, table missing), degrade gracefully to filesystem-only.
    """
    by_slug: dict[str, dict[str, Any]] = {}

    # Filesystem first (seed layer).
    for skill in load_skill_index():
        by_slug[skill["id"]] = {
            "id": skill["id"],
            "name": skill["name"],
            "description": skill["description"],
            "source": "filesystem",
            "requires_connectors": list(skill["requires_connectors"]),
            "spawns_sub_agent": bool(skill["spawns_sub_agent"]),
        }

    # DB skills override by slug; degrade to filesystem-only on error.
    try:
        db_skills = await list_skills_db(org_id)
    except Exception:
        db_skills = []

    for skill in db_skills:
        slug = skill["slug"]
        by_slug[slug] = {
            "id": slug,
            "name": skill["name"],
            "description": skill.get("description") or "",
            "source": skill.get("source") or "uploaded",
            "requires_connectors": list(skill.get("requires_connectors") or []),
            "spawns_sub_agent": bool(skill.get("spawns_sub_agent", False)),
        }

    return list(by_slug.values())


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
