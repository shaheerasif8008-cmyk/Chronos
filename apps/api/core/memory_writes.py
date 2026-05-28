from datetime import datetime, timezone

from typing import Any

from sqlalchemy import insert, or_, select, update
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
    confidence_score: float = 1.0,
    status: str = "active",
    is_sensitive: bool = False,
    provenance: dict[str, Any] | None = None,
    supersedes_memory_id: str | None = None,
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
                confidence_score=max(0.0, min(float(confidence_score), 1.0)),
                status=status,
                is_sensitive=is_sensitive,
                provenance=provenance or {},
                supersedes_memory_id=supersedes_memory_id,
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


def _memory_select_columns(memory_entries) -> list[Any]:
    names = {
        "id",
        "scope",
        "scope_id",
        "content",
        "source",
        "source_conversation_id",
        "importance_score",
        "confidence_score",
        "status",
        "is_pinned",
        "is_archived",
        "is_sensitive",
        "staleness",
        "provenance",
        "conflict_group_id",
        "supersedes_memory_id",
        "created_by",
        "created_at",
        "updated_at",
    }
    return [memory_entries.c[name] for name in names if name in memory_entries.c]


async def list_memory_records(
    member: Member,
    *,
    limit: int = 50,
    offset: int = 0,
    include_archived: bool = False,
    query: str | None = None,
) -> list[dict]:
    memory_entries = await reflect_table("memory_entries")
    stmt = (
        select(*_memory_select_columns(memory_entries))
        .where(
            memory_entries.c.organization_id == member.organization_id,
            memory_entries.c.is_deleted.is_(False),
        )
        .order_by(memory_entries.c.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if "is_archived" in memory_entries.c and not include_archived:
        stmt = stmt.where(memory_entries.c.is_archived.is_(False))
    if query:
        stmt = stmt.where(memory_entries.c.content.ilike(f"%{query}%"))
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(row) for row in rows]


async def update_memory_entry(
    memory_id: str,
    content: str,
    member: Member,
    *,
    importance_score: float | None = None,
    confidence_score: float | None = None,
    scope: str | None = None,
    scope_id: str | None = None,
    status: str | None = None,
    is_pinned: bool | None = None,
    is_archived: bool | None = None,
    is_sensitive: bool | None = None,
    staleness: str | None = None,
    provenance: dict[str, Any] | None = None,
    conflict_group_id: str | None = None,
) -> bool:
    embedding = await embedding_literal_for_memory(
        content,
        actor_id=member.id,
        action="memory.update",
    )
    memory_entries = await reflect_table("memory_entries")
    values = {"content": content, "embedding": embedding, "updated_at": func.now()}
    if importance_score is not None:
        values["importance_score"] = max(0.0, min(float(importance_score), 1.0))
    if confidence_score is not None:
        values["confidence_score"] = max(0.0, min(float(confidence_score), 1.0))
    for key, value in {
        "scope": scope,
        "scope_id": scope_id,
        "status": status,
        "is_pinned": is_pinned,
        "is_archived": is_archived,
        "is_sensitive": is_sensitive,
        "staleness": staleness,
        "provenance": provenance,
        "conflict_group_id": conflict_group_id,
    }.items():
        if value is not None:
            values[key] = value
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


async def archive_memory_entry(memory_id: str, member: Member, *, archived: bool = True) -> bool:
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        result = await conn.execute(
            update(memory_entries)
            .where(
                memory_entries.c.id == memory_id,
                memory_entries.c.organization_id == member.organization_id,
                memory_entries.c.is_deleted.is_(False),
            )
            .values(is_archived=archived, status="archived" if archived else "active", updated_at=func.now())
            .returning(memory_entries.c.id)
        )
        return result.scalar_one_or_none() is not None


async def merge_memory_entries(primary_id: str, merged_ids: list[str], member: Member, *, content: str | None = None) -> bool:
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        primary = (
            await conn.execute(
                select(memory_entries).where(
                    memory_entries.c.id == primary_id,
                    memory_entries.c.organization_id == member.organization_id,
                    memory_entries.c.is_deleted.is_(False),
                )
            )
        ).mappings().first()
        if not primary:
            return False
        group_id = primary.get("conflict_group_id") or primary_id
        values: dict[str, Any] = {
            "conflict_group_id": group_id,
            "status": "active",
            "updated_at": func.now(),
        }
        if content:
            values["content"] = content
            values["embedding"] = await embedding_literal_for_memory(content, actor_id=member.id, action="memory.merge")
        await conn.execute(update(memory_entries).where(memory_entries.c.id == primary_id).values(**values))
        if merged_ids:
            await conn.execute(
                update(memory_entries)
                .where(
                    memory_entries.c.id.in_(merged_ids),
                    memory_entries.c.organization_id == member.organization_id,
                    memory_entries.c.is_deleted.is_(False),
                )
                .values(status="merged", is_archived=True, supersedes_memory_id=primary_id, conflict_group_id=group_id, updated_at=func.now())
            )
    await audit.log("memory_write", member.id, "memory.merge", resource_type="memory", resource_id=primary_id, payload={"merged_ids": merged_ids})
    return True


async def log_memory_access(
    memory_ids: list[str],
    *,
    requester_context: RequesterContext,
    action: str,
    surface: str = "retrieval",
) -> None:
    if not memory_ids:
        return
    try:
        access_logs = await reflect_table("memory_access_logs")
    except Exception:
        return
    payload = {
        "memory_context": requester_context.memory_context,
        "workspace_id": requester_context.workspace_id,
        "project_id": requester_context.project_id,
        "conversation_id": requester_context.conversation_id,
        "task_id": requester_context.task_id,
    }
    async with engine.begin() as conn:
        await conn.execute(
            insert(access_logs),
            [
                {
                    "organization_id": requester_context.org_id,
                    "region": settings.region,
                    "memory_id": memory_id,
                    "actor_id": requester_context.member_id,
                    "action": action,
                    "surface": surface,
                    "request_context": payload,
                }
                for memory_id in memory_ids
            ],
        )


async def list_memory_access_logs(memory_id: str, member: Member, *, limit: int = 50) -> list[dict[str, Any]]:
    access_logs = await reflect_table("memory_access_logs")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(access_logs)
                .where(access_logs.c.organization_id == member.organization_id, access_logs.c.memory_id == memory_id)
                .order_by(access_logs.c.created_at.desc())
                .limit(limit)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def detect_conflicting_memory(content: str, member: Member, *, exclude_id: str | None = None) -> list[dict[str, Any]]:
    memory_entries = await reflect_table("memory_entries")
    words = [word.lower() for word in content.split() if len(word) > 3][:8]
    if not words:
        return []
    conditions = [memory_entries.c.content.ilike(f"%{word}%") for word in words]
    stmt = select(*_memory_select_columns(memory_entries)).where(
        memory_entries.c.organization_id == member.organization_id,
        memory_entries.c.is_deleted.is_(False),
        or_(*conditions),
    ).limit(10)
    if exclude_id:
        stmt = stmt.where(memory_entries.c.id != exclude_id)
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(row) for row in rows if _has_conflict_signal(content, str(row.get("content") or ""))]


def _has_conflict_signal(left: str, right: str) -> bool:
    left_lower = left.lower()
    right_lower = right.lower()
    return (
        (" not " in left_lower or " no longer " in left_lower or "instead" in left_lower)
        or (" not " in right_lower or " no longer " in right_lower or "instead" in right_lower)
    )


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
            update(memory_entries).where(
                memory_entries.c.organization_id == org_id,
                memory_entries.c.scope == "org",
                memory_entries.c.source == "synthesized",
                memory_entries.c.is_deleted.is_(False),
            ).values(status="archived", is_archived=True, updated_at=func.now())
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
                confidence_score=0.8,
                provenance={"source": "profile_synthesis"},
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
