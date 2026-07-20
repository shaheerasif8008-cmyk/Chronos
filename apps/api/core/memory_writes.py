from __future__ import annotations

from sqlalchemy import insert, select, text, update
from sqlalchemy.sql import func

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.embeddings import embed
from core.memory_access import (
    get_memory_for_member,
    memory_access_condition,
    normalize_entry_scope,
)
from core.models import Member, RequesterContext

EXPECTED_EMBEDDING_DIMENSIONS = 1536


class MemoryCaptureDisabled(PermissionError):
    """Raised when the canonical memory privacy policy blocks a write."""


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"


async def embedding_literal_for_memory(
    content: str, *, actor_id: str, action: str, organization_id: str
) -> str | None:
    try:
        vector = await embed(content)
    except Exception as exc:
        await audit.log(
            "memory_embedding_skipped",
            actor_id,
            action,
            organization_id=organization_id,
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
            organization_id=organization_id,
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
    from core.memory_control import is_memory_enabled

    # Keep the authorization boundary at the durable write primitive. API
    # routes validate for friendly errors, but jobs and future callers cannot
    # bypass shared-scope ownership merely by calling this helper directly.
    writer = Member(
        id=requester_context.member_id,
        organization_id=requester_context.org_id,
        email="memory-writer@chronos.invalid",
        role=requester_context.role,
    )
    scope, canonical_scope_id = await normalize_entry_scope(
        writer, scope, scope_id
    )
    scope_id = canonical_scope_id

    effective_project_id = (
        str(scope_id)
        if scope == "project" and scope_id is not None
        else requester_context.project_id
    )
    effective_conversation_id = (
        str(scope_id)
        if scope == "conversation" and scope_id is not None
        else conversation_id or requester_context.conversation_id
    )
    if not await is_memory_enabled(
        org_id=requester_context.org_id,
        project_id=effective_project_id,
        member_id=requester_context.member_id,
        conversation_id=effective_conversation_id,
    ):
        await audit.log(
            "memory_write_blocked",
            requester_context.member_id,
            f"memory.{source}",
            organization_id=requester_context.org_id,
            resource_type="memory",
            payload={
                "scope": scope,
                "project_id": effective_project_id,
                "conversation_id": effective_conversation_id,
            },
            decision="memory_disabled",
        )
        raise MemoryCaptureDisabled("Memory is disabled for this context")

    embedding = await embedding_literal_for_memory(
        content,
        actor_id=requester_context.member_id,
        action=f"memory.{source}",
        organization_id=requester_context.org_id,
    )
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(memory_entries)
            .values(
                organization_id=requester_context.org_id,
                region=settings.region,
                scope=scope,
                scope_id=scope_id,
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
        organization_id=requester_context.org_id,
        resource_type="memory",
        resource_id=entry_id,
        payload={"source": source, "scope": scope},
    )
    return entry_id


async def list_memory_records(member: Member, *, limit: int = 50, offset: int = 0) -> list[dict]:
    memory_entries = await reflect_table("memory_entries")
    visible = await memory_access_condition(memory_entries, member)
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
                    visible,
                )
                .order_by(memory_entries.c.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def update_memory_entry(memory_id: str, content: str, member: Member, *, importance_score: float | None = None) -> bool:
    memory = await get_memory_for_member(memory_id, member, mutate=True)
    if memory is None:
        return False
    embedding = await embedding_literal_for_memory(
        content,
        actor_id=member.id,
        action="memory.update",
        organization_id=member.organization_id,
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
                memory_entries.c.scope == memory["scope"],
                memory_entries.c.scope_id == memory["scope_id"],
                memory_entries.c.is_deleted.is_(False),
            )
            .values(**values)
            .returning(memory_entries.c.id)
        )
        return result.scalar_one_or_none() is not None


async def soft_delete_memory_entry(memory_id: str, member: Member) -> bool:
    memory = await get_memory_for_member(memory_id, member, mutate=True)
    if memory is None:
        return False
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        result = await conn.execute(
            update(memory_entries)
            .where(
                memory_entries.c.id == memory_id,
                memory_entries.c.organization_id == member.organization_id,
                memory_entries.c.scope == memory["scope"],
                memory_entries.c.scope_id == memory["scope_id"],
                memory_entries.c.is_deleted.is_(False),
            )
            .values(is_deleted=True, updated_at=func.now())
            .returning(memory_entries.c.id)
        )
        return result.scalar_one_or_none() is not None


async def replace_synthesized_memory_entry(*, org_id: str, content: str, embedding: str | None) -> str:
    """Reject the retired direct-to-organization synthesis path.

    Organization context derived by a model must be staged through the pending
    context-suggestion workflow and explicitly approved. Keeping this guard at
    the old persistence seam prevents a future caller from silently restoring
    the pre-review behavior.
    """
    del org_id, content, embedding
    raise RuntimeError(
        "direct synthesized organization memory is disabled; create a reviewed context suggestion"
    )


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
    memory = await get_memory_for_member(memory_id, member, mutate=True)
    if memory is None:
        return False
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(
                    memory_entries.c.id,
                ).where(
                    memory_entries.c.id == memory_id,
                    memory_entries.c.organization_id == member.organization_id,
                    memory_entries.c.scope == memory["scope"],
                    memory_entries.c.scope_id == memory["scope_id"],
                    memory_entries.c.source == "autonomous",
                    memory_entries.c.is_deleted.is_(False),
                    memory_entries.c.created_at
                    >= func.now() - text("INTERVAL '60 seconds'"),
                )
            )
        ).mappings().first()
        if row is None:
            return False

    from core import retention

    if not await retention.soft_delete_memory_if_allowed(memory_id, member):
        return False
    await audit.log(
        "memory_undo",
        member.id,
        "memory.undo_autonomous",
        organization_id=member.organization_id,
        resource_type="memory",
        resource_id=memory_id,
    )
    return True
