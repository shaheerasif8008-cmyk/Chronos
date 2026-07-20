"""Global search router — unified search across conversations, messages, tasks,
artifacts, memory, and (future) project sources.

All results are org-scoped. Conversations and messages are additionally
member-scoped (member owns the conversation).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Text, and_, cast, or_, select

from core import audit, permissions
from core.auth import get_current_member
from core.conversation_access import ADMIN_ROLES, visibility_clause as conversation_visibility
from core.db import engine, reflect_table
from core.memory_access import memory_access_condition
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


def _relevance(hit: dict[str, Any], q_lower: str) -> int:
    """Higher is more relevant. Title matches outrank body-only matches, and
    exact/prefix title matches outrank substring matches, so the merged result
    set reads as a single ranked list rather than per-type buckets."""
    title = (hit.get("title") or "").lower()
    snippet = (hit.get("snippet") or "").lower()
    if title == q_lower:
        return 100
    if title.startswith(q_lower):
        return 70
    if q_lower in title:
        return 50
    if q_lower in snippet:
        return 20
    return 10


def _rank_key(hit: dict[str, Any], q_lower: str):
    """Sort by relevance, then recency (newer first). ``created_at``/``updated_at``
    are stringified timestamps where present; absent values sort last."""
    ts = hit.get("updated_at") or hit.get("created_at") or ""
    return (_relevance(hit, q_lower), ts)


async def _search_conversations(q: str, member: Member) -> list[dict[str, Any]]:
    escaped = _escape_like(q)
    tbl = await reflect_table("conversations")
    conditions = [
        tbl.c.organization_id == member.organization_id,
        tbl.c.title.ilike(f"%{escaped}%", escape="\\"),
    ]
    if member.role not in ADMIN_ROLES:
        try:
            acl = await reflect_table("conversation_members")
        except Exception:
            acl = None
        conditions.append(
            conversation_visibility(tbl, acl, member)
            if acl is not None
            else tbl.c.member_id == member.id
        )
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(tbl.c.id, tbl.c.title, tbl.c.created_at, tbl.c.updated_at)
                .where(and_(*conditions))
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
            "created_at": str(row["created_at"]) if row["created_at"] is not None else None,
            "updated_at": str(row["updated_at"]) if row["updated_at"] is not None else None,
        }
        for row in rows
    ]


async def _search_messages(q: str, member: Member) -> list[dict[str, Any]]:
    escaped = _escape_like(q)
    msgs = await reflect_table("messages")
    convos = await reflect_table("conversations")
    conditions = [
        msgs.c.organization_id == member.organization_id,
        convos.c.organization_id == member.organization_id,
        msgs.c.content.ilike(f"%{escaped}%", escape="\\"),
    ]
    if member.role not in ADMIN_ROLES:
        try:
            acl = await reflect_table("conversation_members")
        except Exception:
            acl = None
        conditions.append(
            conversation_visibility(convos, acl, member)
            if acl is not None
            else convos.c.member_id == member.id
        )
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    msgs.c.id,
                    msgs.c.content,
                    msgs.c.conversation_id,
                    msgs.c.created_at,
                    convos.c.title.label("conversation_title"),
                    convos.c.updated_at.label("conversation_updated_at"),
                )
                .select_from(msgs.join(convos, msgs.c.conversation_id == convos.c.id))
                .where(and_(*conditions))
                .order_by(msgs.c.created_at.desc())
                .limit(_PER_TYPE_LIMIT)
            )
        ).mappings().all()
    return [
        {
            "type": "messages",
            "id": str(row["id"]),
            "title": row["conversation_title"] or _snippet(row["content"], max_len=60),
            "snippet": _snippet(row["content"]),
            "url": f"/chat?c={row['conversation_id']}",
            "created_at": str(row["created_at"]) if row["created_at"] is not None else None,
            "updated_at": str(row["conversation_updated_at"]) if row["conversation_updated_at"] is not None else None,
        }
        for row in rows
    ]


async def _search_tasks(q: str, member: Member) -> list[dict[str, Any]]:
    escaped = _escape_like(q)
    tbl = await reflect_table("tasks")
    conditions = [
        tbl.c.organization_id == member.organization_id,
        tbl.c.goal.ilike(f"%{escaped}%", escape="\\"),
    ]
    if member.role not in {"admin", "owner"}:
        conditions.append(
            or_(
                tbl.c.triggered_by_member_id == member.id,
                tbl.c.assignee_member_id == member.id,
            )
        )
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(tbl.c.id, tbl.c.goal, tbl.c.status, tbl.c.created_at)
                .where(
                    and_(*conditions)
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
            "created_at": str(row["created_at"]) if row["created_at"] is not None else None,
        }
        for row in rows
    ]


async def _search_artifacts(q: str, member: Member) -> list[dict[str, Any]]:
    escaped = _escape_like(q)
    tbl = await reflect_table("artifacts")
    conditions = [
        tbl.c.organization_id == member.organization_id,
        tbl.c.is_deleted.is_(False),
        tbl.c.title.ilike(f"%{escaped}%", escape="\\"),
    ]
    if member.role not in {"admin", "owner"}:
        conversations = await reflect_table("conversations")
        conversation_members = await reflect_table("conversation_members")
        tasks = await reflect_table("tasks")
        project_members = await reflect_table("project_members")
        member_projects = select(project_members.c.project_id).where(
            project_members.c.organization_id == member.organization_id,
            project_members.c.member_id == member.id,
        )
        member_conversations = select(conversations.c.id).where(
            conversations.c.organization_id == member.organization_id,
            or_(
                conversations.c.member_id == member.id,
                select(conversation_members.c.id)
                .where(
                    conversation_members.c.organization_id == member.organization_id,
                    conversation_members.c.conversation_id == conversations.c.id,
                    conversation_members.c.member_id == member.id,
                )
                .exists(),
            ),
        )
        member_tasks = select(tasks.c.id).where(
            tasks.c.organization_id == member.organization_id,
            or_(
                tasks.c.triggered_by_member_id == member.id,
                tasks.c.assignee_member_id == member.id,
            ),
        )
        conditions.append(
            or_(
                tbl.c.created_by.in_([str(member.id), f"member:{member.id}"]),
                tbl.c.project_id.in_(member_projects),
                cast(tbl.c.conversation_id, Text).in_(member_conversations),
                cast(tbl.c.task_id, Text).in_(member_tasks),
            )
        )
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(tbl.c.id, tbl.c.title, tbl.c.kind, tbl.c.created_at)
                .where(
                    and_(*conditions)
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
            "created_at": str(row["created_at"]) if row["created_at"] is not None else None,
        }
        for row in rows
    ]


async def _search_memory(q: str, member: Member) -> list[dict[str, Any]]:
    """Search every durable memory scope the caller may currently read.

    A bare chat RequesterContext cannot enumerate all authorized projects,
    conversations, tasks, and personas. Global search therefore uses the same
    canonical read predicate as the Memory control center, with literal text
    matching rather than silently dropping those scopes.
    """
    escaped = _escape_like(q)
    table = await reflect_table("memory_entries")
    visible = await memory_access_condition(table, member)
    async with engine.begin() as conn:
        entries = (
            await conn.execute(
                select(
                    table.c.id,
                    table.c.content,
                    table.c.created_at,
                    table.c.updated_at,
                )
                .where(
                    table.c.organization_id == member.organization_id,
                    table.c.is_deleted.is_(False),
                    table.c.is_archived.is_(False),
                    table.c.superseded_by.is_(None),
                    table.c.source != "synthesized",
                    table.c.content.ilike(f"%{escaped}%", escape="\\"),
                    visible,
                )
                .order_by(table.c.is_pinned.desc(), table.c.updated_at.desc())
                .limit(_PER_TYPE_LIMIT)
            )
        ).mappings().all()
    return [
        {
            "type": "memory",
            "id": str(entry["id"]),
            "title": _snippet(entry["content"], max_len=60),
            "snippet": _snippet(entry["content"]),
            "url": "/memory",
            "created_at": str(entry["created_at"]) if entry.get("created_at") else None,
            "updated_at": str(entry["updated_at"]) if entry.get("updated_at") else None,
        }
        for entry in entries
    ]


async def _search_sources(q: str, member: Member) -> list[dict[str, Any]]:
    """Search project sources the caller can see.

    Sources are scoped to projects the member belongs to (via ``project_members``),
    not merely to the org — an org peer who is not a project member must not see
    that project's sources in search results. Degrades to ``[]`` if the tables do
    not exist yet (older checkouts).
    """
    escaped = _escape_like(q)
    try:
        tbl = await reflect_table("project_sources")
        chunks = await reflect_table("project_source_chunks")
        members_tbl = await reflect_table("project_members")
    except Exception:
        # Table does not exist yet (arrives in a later sprint)
        return []
    try:
        async with engine.begin() as conn:
            candidate_limit = _PER_TYPE_LIMIT * 6
            rows = (
                await conn.execute(
                    select(
                        tbl.c.id,
                        tbl.c.title,
                        tbl.c.project_id,
                        tbl.c.created_at,
                        tbl.c.permissions,
                        tbl.c.created_by,
                        tbl.c.index_status,
                        chunks.c.content.label("matched_content"),
                        members_tbl.c.role.label("project_role"),
                    )
                    .select_from(
                        tbl.join(
                            members_tbl,
                            and_(
                                tbl.c.project_id == members_tbl.c.project_id,
                                tbl.c.organization_id == members_tbl.c.organization_id,
                            ),
                        ).outerjoin(
                            chunks,
                            and_(
                                chunks.c.source_id == tbl.c.id,
                                chunks.c.organization_id == tbl.c.organization_id,
                                chunks.c.project_id == tbl.c.project_id,
                            ),
                        )
                    )
                    .where(
                        and_(
                            tbl.c.organization_id == member.organization_id,
                            members_tbl.c.organization_id == member.organization_id,
                            members_tbl.c.member_id == member.id,
                            tbl.c.index_status.in_(["indexed", "synced"]),
                            or_(
                                tbl.c.title.ilike(f"%{escaped}%", escape="\\"),
                                chunks.c.content.ilike(f"%{escaped}%", escape="\\"),
                            ),
                        )
                    )
                    .order_by(tbl.c.created_at.desc())
                    .limit(candidate_limit)
                )
            ).mappings().all()
        from memory.source_retrieval import source_permissions_allow

        context = RequesterContext.from_member(member)
        visible_rows = []
        seen_source_ids: set[str] = set()
        for row in rows:
            source_id = str(row["id"])
            if source_id in seen_source_ids:
                continue
            context.project_id = str(row["project_id"])
            if source_permissions_allow(
                row.get("permissions"),
                context,
                created_by=row.get("created_by"),
            ):
                visible_rows.append(row)
                seen_source_ids.add(source_id)
            if len(visible_rows) >= _PER_TYPE_LIMIT:
                break
        return [
            {
                "type": "sources",
                "id": str(row["id"]),
                "title": row["title"] or "Untitled source",
                "snippet": _snippet(row.get("matched_content") or row["title"]),
                "url": (
                    f"/projects?id={row['project_id']}&tab=sources"
                    f"&source={row['id']}"
                ),
                "created_at": str(row["created_at"]) if row["created_at"] is not None else None,
            }
            for row in visible_rows
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
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Core search logic — extracted so tests can call it without HTTP machinery.

    Results from all requested types are merged and ranked into a single list
    (most relevant first). ``limit`` optionally caps the total returned after
    ranking; ``None`` returns every match.
    """
    await permissions.check(member, "search", "global")
    await audit.log(
        "search",
        member.id,
        "search.global",
        organization_id=member.organization_id,
        resource_type="global",
        payload={
            "query_sha256": hashlib.sha256(q.encode("utf-8")).hexdigest(),
            "query_length": len(q),
            "types": types_csv or "all",
        },
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

    q_lower = q.lower()
    results.sort(key=lambda hit: _rank_key(hit, q_lower), reverse=True)

    if limit is not None:
        results = results[:limit]

    return results


@router.get("")
async def search(
    q: str = Query(..., min_length=1, max_length=200),
    types: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=100),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    """Search across conversations, messages, tasks, artifacts, memory, and sources.

    Args:
        q: Required search term (1–200 characters).
        types: Optional comma-separated list of result types to include.
               Defaults to all types. Unknown types are silently ignored.
        limit: Optional cap on the total number of ranked results (1–100).

    Returns:
        Unified, relevance-ranked list of hits: {type, id, title, snippet, url}.
    """
    return await run_search(q=q, types_csv=types, member=member, limit=limit)
