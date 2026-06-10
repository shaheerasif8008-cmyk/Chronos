"""SQLAlchemy Core table definitions for DB-persisted, versioned skills.

These mirror migration ``0032_skills_persistence``. Runtime queries use
``core.db.reflect_table`` like the rest of the codebase; these Table objects
document the schema and back the unit tests' in-memory SQLite setup.
"""
from __future__ import annotations

from sqlalchemy import (
    ARRAY,
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
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()


skills = Table(
    "skills",
    metadata,
    Column("id", UUID(as_uuid=False), primary_key=True),
    Column("organization_id", Text, nullable=False, server_default="default"),
    Column("region", Text, nullable=False, server_default="us"),
    Column("slug", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=True),
    Column("requires_connectors", ARRAY(Text), nullable=False, server_default="{}"),
    Column("spawns_sub_agent", Boolean, nullable=False, server_default="false"),
    Column("source", Text, nullable=False, server_default="filesystem"),
    Column("current_version", Integer, nullable=False, server_default="1"),
    Column("is_deleted", Boolean, nullable=False, server_default="false"),
    Column("created_at", TIMESTAMP(timezone=True), server_default=func.now()),
    Column("updated_at", TIMESTAMP(timezone=True), server_default=func.now()),
    UniqueConstraint("organization_id", "slug", name="uq_skills_org_slug"),
)


skill_versions = Table(
    "skill_versions",
    metadata,
    Column("id", UUID(as_uuid=False), primary_key=True),
    Column("organization_id", Text, nullable=False, server_default="default"),
    Column("region", Text, nullable=False, server_default="us"),
    Column("skill_id", UUID(as_uuid=False), ForeignKey("skills.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("content", Text, nullable=True),
    Column("metadata", JSONB, nullable=False, server_default="{}"),
    Column("created_at", TIMESTAMP(timezone=True), server_default=func.now()),
    Column("created_by", Text, nullable=True),
    UniqueConstraint("skill_id", "version", name="uq_skill_versions_skill_version"),
)
