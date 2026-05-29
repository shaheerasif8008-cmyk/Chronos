"""Global search router — unified search across conversations, messages, tasks,
artifacts, memory, and (future) project sources.

All results are org-scoped. Conversations and messages are additionally
member-scoped (member owns the conversation).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, select

from core import audit, memory, permissions
from core.auth import get_current_member
from core.db import engine, reflect_table
from core.models import Member, RequesterContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

_ALL_TYPES = ["conversations", "messages", "tasks", "artifacts", "memory", "sources"]
_PER_TYPE_LIMIT = 10


def _escape_like(q: str) -> str:
    """Escape LIKE metacharacters so user input is treated as a literal string."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet(text: str | None, *, max_len: int = 200) -> str:
    if not text:
        return ""
    text = text.strip()
    return text[:max_len] + ("…" if len(text) > max_len else "")


async def _search_conversations(q: str, member: Member) -> list[dict[str, Any]]:
    escaped = _escape_like(q)
    tbl = await reflect_table("conversations")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(tbl.c.id, tbl.c.title, tbl.c.updated_at)
                .where(
                    and_(
                        tbl.c.organization_id == member.organization_id,
                        tbl.c.member_id == member.id,
                        tbl.c.title.ilike(f"%{escaped}%", escape="\\"),
                    )
                )
                .order_by(tbl.c.updated_at.desc())
                .limit(_PER_TYPE_LIMIT)
            )
        ).mappings().all()
    return [
        {
            "type": "conversations",
            "id": str(row["id"]),
            "title": row["title"] or "Untitled",
            "snippet": _snippet(row["title"]),
            "url": f"/chat?c={row['id']}",
        }
        for row in rows
    ]


async def _search_messages(q: str, member: Member) -> list[dict[str, Any]]:
    escaped = _escape_like(q)
    msgs = await reflect_table("messages")
    convos = await reflect_table("conversations")
    # Subquery: IDs of conversations owned by this member in this org
    owned_convo_ids = (
        select(convos.c.id)
        .where(
            and_(
                convos.c.organization_id == member.organization_id,
                convos.c.member_id == member.id,
            )
        )
    )
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(msgs.c.id, msgs.c.content, msgs.c.conversation_id, msgs.c.created_at)
                .where(
                    and_(
                        msgs.c.organization_id == member.organization_id,
                        msgs.c.conversation_id.in_(owned_convo_ids),
                        msgs.c.content.ilike(f"%{escaped}%", escape="\\"),
                    )
                )
                .order_by(msgs.c.created_at.desc())
                .limit(_PER_TYPE_LIMIT)
            )
        ).mappings().all()
    return [
        {
            "type": "messages",
            "id": str(row["id"]),
            "title": _snippet(row["content"], max_len=60),
            "snippet": _snippet(row["content"]),
            "url": f"/chat?c={row['conversation_id']}",
        }
        for row in rows
    ]


async def _search_tasks(q: str, member: Member) -> list[dict[str, Any]]:
    escaped = _escape_like(q)
    tbl = await reflect_table("tasks")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(tbl.c.id, tbl.c.goal, tbl.c.status, tbl.c.created_at)
                .where(
                    and_(
                        tbl.c.organization_id == member.organization_id,
                        tbl.c.goal.ilike(f"%{escaped}%", escape="\\"),
                    )
                )
                .order_by(tbl.c.created_at.desc())
                .limit(_PER_TYPE_LIMIT)
            )
        ).mappings().all()
    return [
        {
            "type": "tasks",
            "id": str(row["id"]),
            "title": _snippet(row["goal"], max_len=80),
            "snippet": _snippet(row["goal"]),
            "url": "/tasks",
        }
        for row in rows
    ]


async def _search_artifacts(q: str, member: Member) -> list[dict[str, Any]]:
    escaped = _escape_like(q)
    tbl = await reflect_table("artifacts")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(tbl.c.id, tbl.c.title, tbl.c.kind, tbl.c.created_at)
                .where(
                    and_(
                        tbl.c.organization_id == member.organization_id,
                        tbl.c.title.ilike(f"%{escaped}%", escape="\\"),
                    )
                )
                .order_by(tbl.c.created_at.desc())
                .limit(_PER_TYPE_LIMIT)
            )
        ).mappings().all()
    return [
        {
            "type": "artifacts",
            "id": str(row["id"]),
            "title": row["title"] or "Untitled artifact",
            "snippet": _snippet(row["title"]),
            "url": "/artifacts",
        }
        for row in rows
    ]


async def _search_memory(q: str, member: Member) -> list[dict[str, Any]]:
    """Memory search always goes through the retrieve seam — never raw SQL."""
    ctx = RequesterContext.from_member(member)
    entries = await memory.retrieve(q, ctx)
    return [
        {
            "type": "memory",
            "id": entry.id,
            "title": _snippet(entry.content, max_len=60),
            "snippet": _snippet(entry.content),
            "url": "/memory",
        }
        for entry in entries
    ]


async def _search_sources(q: str, member: Member) -> list[dict[str, Any]]:
    """Search project_sources if the table exists; degrade to [] if not."""
    escaped = _escape_like(q)
    try:
        tbl = await reflect_table("project_sources")
    except Exception:
        # Table does not exist yet (arrives in a later sprint)
        return []
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(tbl.c.id, tbl.c.title)
                    .where(
                        and_(
                            tbl.c.organization_id == member.organization_id,
                            tbl.c.title.ilike(f"%{escaped}%", escape="\\"),
                        )
                    )
                    .order_by(tbl.c.created_at.desc())
                    .limit(_PER_TYPE_LIMIT)
                )
            ).mappings().all()
        return [
            {
                "type": "sources",
                "id": str(row["id"]),
                "title": row["title"] or "Untitled source",
                "snippet": _snippet(row["title"]),
                "url": "/projects",
            }
            for row in rows
        ]
    except Exception:
        return []


_HANDLERS: dict[str, Any] = {
    "conversations": _search_conversations,
    "messages": _search_messages,
    "tasks": _search_tasks,
    "artifacts": _search_artifacts,
    "memory": _search_memory,
    "sources": _search_sources,
}


# Public helper used by tests (avoids importing FastAPI machinery)
async def run_search(
    *,
    q: str,
    types_csv: str | None,
    member: Member,
) -> list[dict[str, Any]]:
    """Core search logic — extracted so tests can call it without HTTP machinery."""
    await permissions.check(member, "search", "global")
    await audit.log(
        "search",
        member.id,
        "search.global",
        resource_type="global",
        payload={"q_preview": q[:120], "types": types_csv or "all"},
    )

    q = q.strip()

    requested = _ALL_TYPES[:]
    if types_csv:
        parsed = [t.strip().lower() for t in types_csv.split(",") if t.strip()]
        known = set(_ALL_TYPES)
        requested = [t for t in parsed if t in known]
        if not requested:
            return []

    async def _run_handler(type_name: str) -> list[dict[str, Any]]:
        try:
            return await _HANDLERS[type_name](q, member)
        except Exception:
            logger.exception("Search handler %r failed", type_name)
            return []

    nested = await asyncio.gather(*[_run_handler(t) for t in requested])
    results: list[dict[str, Any]] = []
    for hits in nested:
        results.extend(hits)

    return results


@router.get("")
async def search(
    q: str = Query(..., min_length=1, max_length=200),
    types: str | None = None,
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    """Search across conversations, messages, tasks, artifacts, memory, and sources.

    Args:
        q: Required search term (1–200 characters).
        types: Optional comma-separated list of result types to include.
               Defaults to all types. Unknown types are silently ignored.

    Returns:
        Unified list of search hits: {type, id, title, snippet, url}.
    """
    return await run_search(q=q, types_csv=types, member=member)
