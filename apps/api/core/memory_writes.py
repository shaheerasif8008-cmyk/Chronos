from __future__ import annotations
from datetime import datetime, timezone

from sqlalchemy import delete, insert, select, update
from sqlalchemy.sql import func

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.embeddings import embed
from core.models import Member, RequesterContext

EXPECTED_EMBEDDING_DIMENSIONS = 1536


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"


async def embedding_literal_for_memory(content: str, *, actor_id: str, action: str) -> str | None:
    try:
        vector = await embed(content)
    except Exception as exc:
        await audit.log(
            "memory_embedding_skipped",
            actor_id,
            action,
            resource_type="memory_entries",
            payload={"error": str(exc)[:240]},
            decision="embedding_failed",
        )
        return None
    if len(vector) != EXPECTED_EMBEDDING_DIMENSIONS:
        await audit.log(
            "memory_embedding_skipped",
            actor_id,
            action,
            resource_type="memory_entries",
            payload={
                "expected_dimensions": EXPECTED_EMBEDDING_DIMENSIONS,
                "actual_dimensions": len(vector),
            },
            decision="dimension_mismatch",
        )
        return None
    return vector_literal(vector)


async def create_memory_entry(
    *,
    content: str,
    requester_context: RequesterContext,
    source: str,
    scope: str = "org",
    scope_id: str | None = None,
    importance_score: float = 0.8,
    conversation_id: str | None = None,
    created_by: str | None = None,
) -> str:
    embedding = await embedding_literal_for_memory(
        content,
        actor_id=requester_context.member_id,
        action=f"memory.{source}",
    )
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(memory_entries)
            .values(
                organization_id=requester_context.org_id,
                region=settings.region,
                scope=scope,
                scope_id=scope_id or requester_context.org_id,
                content=content,
                embedding=embedding,
                source=source,
                source_conversation_id=conversation_id,
                importance_score=importance_score,
                created_by=created_by or requester_context.member_id,
            )
            .returning(memory_entries.c.id)
        )
        entry_id = str(result.scalar_one())
    await audit.log(
        "memory_write",
        requester_context.member_id,
        f"memory.{source}",
        resource_type="memory",
        resource_id=entry_id,
        payload={"source": source, "scope": scope},
    )
    return entry_id


async def list_memory_records(member: Member, *, limit: int = 50, offset: int = 0) -> list[dict]:
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    memory_entries.c.id,
                    memory_entries.c.scope,
                    memory_entries.c.scope_id,
                    memory_entries.c.content,
                    memory_entries.c.source,
                    memory_entries.c.importance_score,
                    memory_entries.c.created_by,
                    memory_entries.c.created_at,
                    memory_entries.c.updated_at,
                )
                .where(
                    memory_entries.c.organization_id == member.organization_id,
                    memory_entries.c.is_deleted.is_(False),
                )
                .order_by(memory_entries.c.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def update_memory_entry(memory_id: str, content: str, member: Member, *, importance_score: float | None = None) -> bool:
    embedding = await embedding_literal_for_memory(
        content,
        actor_id=member.id,
        action="memory.update",
    )
    memory_entries = await reflect_table("memory_entries")
    values = {"content": content, "embedding": embedding, "updated_at": func.now()}
    if importance_score is not None:
        values["importance_score"] = max(0.0, min(float(importance_score), 1.0))
    async with engine.begin() as conn:
        result = await conn.execute(
            update(memory_entries)
            .where(
                memory_entries.c.id == memory_id,
                memory_entries.c.organization_id == member.organization_id,
                memory_entries.c.is_deleted.is_(False),
            )
            .values(**values)
            .returning(memory_entries.c.id)
        )
        return result.scalar_one_or_none() is not None


async def soft_delete_memory_entry(memory_id: str, member: Member) -> bool:
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        result = await conn.execute(
            update(memory_entries)
            .where(
                memory_entries.c.id == memory_id,
                memory_entries.c.organization_id == member.organization_id,
                memory_entries.c.is_deleted.is_(False),
            )
            .values(is_deleted=True, updated_at=func.now())
            .returning(memory_entries.c.id)
        )
        return result.scalar_one_or_none() is not None


async def replace_synthesized_memory_entry(*, org_id: str, content: str, embedding: str | None) -> str:
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        await conn.execute(
            delete(memory_entries).where(
                memory_entries.c.organization_id == org_id,
                memory_entries.c.scope == "org",
                memory_entries.c.source == "synthesized",
            )
        )
        result = await conn.execute(
            insert(memory_entries)
            .values(
                organization_id=org_id,
                region=settings.region,
                scope="org",
                scope_id=org_id,
                content=content,
                embedding=embedding,
                source="synthesized",
                importance_score=0.9,
                created_by="chronos",
            )
            .returning(memory_entries.c.id)
        )
        return str(result.scalar_one())


def extract_explicit_memory_content(message: str) -> str | None:
    normalized = message.strip()
    lowered = normalized.lower()
    prefixes = (
        "remember that ",
        "remember this: ",
        "remember: ",
        "please remember that ",
        "please remember: ",
    )
    for prefix in prefixes:
        if lowered.startswith(prefix):
            content = normalized[len(prefix) :].strip()
            return content or None
    return None


async def undo_autonomous_memory(memory_id: str, member: Member) -> bool:
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(
                    memory_entries.c.id,
                    memory_entries.c.created_at,
                ).where(
                    memory_entries.c.id == memory_id,
                    memory_entries.c.organization_id == member.organization_id,
                    memory_entries.c.source == "autonomous",
                    memory_entries.c.is_deleted.is_(False),
                )
            )
        ).mappings().first()
        if row is None:
            return False

        created_at = row["created_at"]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()
        if age_seconds > 60:
            return False

        result = await conn.execute(
            update(memory_entries)
            .where(
                memory_entries.c.id == memory_id,
                memory_entries.c.organization_id == member.organization_id,
                memory_entries.c.source == "autonomous",
                memory_entries.c.is_deleted.is_(False),
            )
            .values(is_deleted=True)
            .returning(memory_entries.c.id)
        )
        undone = result.scalar_one_or_none()
    if undone is None:
        return False
    await audit.log(
        "memory_undo",
        member.id,
        "memory.undo_autonomous",
        resource_type="memory",
        resource_id=memory_id,
    )
    return True
