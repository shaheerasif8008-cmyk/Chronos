from datetime import datetime, timezone

from sqlalchemy import insert, select, update

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.embeddings import embed
from core.models import Member, RequesterContext


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"


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
    vector = await embed(content)
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
                embedding=vector_literal(vector),
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
        resource_type="memory_entries",
        resource_id=entry_id,
        payload={"source": source, "scope": scope},
    )
    return entry_id


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
        resource_type="memory_entries",
        resource_id=memory_id,
    )
    return True
