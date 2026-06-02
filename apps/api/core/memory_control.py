"""Memory control center, conflict/staleness, and privacy controls (Phase 4).

This module builds the enterprise memory *product* on top of the frozen
``memory.retrieve`` seam and the ``memory_writes`` primitives:

- Control center: archive, pin, classify-sensitive, change-scope, merge, search,
  import/export, and usage logs.
- Conflict / staleness: deterministic near-duplicate detection (token overlap)
  with supersession so older facts stop being retrieved.
- Privacy: per-project / per-member / per-conversation memory disable, stored in
  the existing ``settings_documents`` table (section ``memory_policy``).

All writes are tenant-scoped on ``organization_id`` and audit-logged.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.sql import func

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member, RequesterContext

# Two memories in the same scope are treated as conflicting near-duplicates when
# their content token sets overlap at least this much (Jaccard). Deterministic,
# not provider-bound — matches the retrieval ranker's dedupe philosophy.
CONFLICT_SIMILARITY_THRESHOLD = 0.5


# --------------------------------------------------------------------------- #
# Privacy controls
# --------------------------------------------------------------------------- #
async def _policy_disabled(org_id: str, scope: str, scope_id: str) -> bool:
    table = await reflect_table("settings_documents")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table.c["values"]).where(
                    table.c.organization_id == org_id,
                    table.c.scope == scope,
                    table.c.scope_id == scope_id,
                    table.c.section == "memory_policy",
                )
            )
        ).first()
    if not row:
        return False
    values = row[0] or {}
    return values.get("enabled", True) is False


async def is_memory_enabled(
    *,
    org_id: str,
    project_id: str | None = None,
    member_id: str | None = None,
    conversation_id: str | None = None,
) -> bool:
    """False if memory is disabled at the org, project, member, or conversation level."""
    checks = [("org", org_id)]
    if project_id:
        checks.append(("project", project_id))
    if member_id:
        checks.append(("member", member_id))
    if conversation_id:
        checks.append(("conversation", conversation_id))
    for scope, scope_id in checks:
        if await _policy_disabled(org_id, scope, scope_id):
            return False
    return True


async def set_memory_policy(member: Member, *, scope: str, scope_id: str, enabled: bool) -> dict[str, Any]:
    """Enable/disable memory for a scope (org|project|member|conversation)."""
    from core.settings_store import save_settings_doc

    if scope not in {"org", "project", "member", "conversation"}:
        raise ValueError(f"invalid memory policy scope: {scope}")
    doc = await save_settings_doc(
        member, "memory_policy", {"enabled": enabled}, scope=scope, scope_id=scope_id
    )
    await audit.log(
        "memory_policy",
        member.id,
        "memory.policy.set",
        resource_type="memory_policy",
        resource_id=f"{scope}:{scope_id}",
        payload={"enabled": enabled},
        decision="enabled" if enabled else "disabled",
    )
    return doc


# --------------------------------------------------------------------------- #
# Usage logs
# --------------------------------------------------------------------------- #
async def record_memory_usage(
    memory_ids: list[str],
    *,
    requester_context: RequesterContext,
    context: str | None = None,
) -> None:
    """Append-only record that these memories were surfaced for a request."""
    if not memory_ids:
        return
    table = await reflect_table("memory_usage_log")
    rows = [
        {
            "organization_id": requester_context.org_id,
            "region": settings.region,
            "memory_id": str(mid),
            "task_id": requester_context.task_id,
            "used_by": requester_context.member_id,
            "context": context,
        }
        for mid in memory_ids
    ]
    async with engine.begin() as conn:
        await conn.execute(insert(table), rows)


async def list_memory_usage(memory_id: str, member: Member, *, limit: int = 50) -> list[dict[str, Any]]:
    table = await reflect_table("memory_usage_log")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(table)
                .where(
                    table.c.memory_id == memory_id,
                    table.c.organization_id == member.organization_id,
                )
                .order_by(table.c.created_at.desc())
                .limit(limit)
            )
        ).mappings().all()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Control-center flag updates
# --------------------------------------------------------------------------- #
async def _set_fields(memory_id: str, org_id: str, **values: Any) -> bool:
    table = await reflect_table("memory_entries")
    values["updated_at"] = func.now()
    async with engine.begin() as conn:
        result = await conn.execute(
            update(table)
            .where(
                table.c.id == memory_id,
                table.c.organization_id == org_id,
                table.c.is_deleted.is_(False),
            )
            .values(**values)
            .returning(table.c.id)
        )
        return result.scalar_one_or_none() is not None


async def archive_memory(memory_id: str, member: Member, *, archived: bool = True) -> bool:
    ok = await _set_fields(memory_id, member.organization_id, is_archived=archived)
    if ok:
        await audit.log(
            "memory_write", member.id, "memory.archive" if archived else "memory.unarchive",
            resource_type="memory", resource_id=memory_id,
        )
    return ok


async def set_pinned(memory_id: str, member: Member, *, pinned: bool) -> bool:
    ok = await _set_fields(memory_id, member.organization_id, is_pinned=pinned)
    if ok:
        await audit.log(
            "memory_write", member.id, "memory.pin" if pinned else "memory.unpin",
            resource_type="memory", resource_id=memory_id,
        )
    return ok


async def set_sensitive(memory_id: str, member: Member, *, sensitive: bool) -> bool:
    ok = await _set_fields(memory_id, member.organization_id, is_sensitive=sensitive)
    if ok:
        await audit.log(
            "memory_write", member.id, "memory.classify_sensitive",
            resource_type="memory", resource_id=memory_id,
            payload={"sensitive": sensitive},
        )
    return ok


async def change_scope(memory_id: str, member: Member, *, scope: str, scope_id: str) -> bool:
    ok = await _set_fields(memory_id, member.organization_id, scope=scope, scope_id=scope_id)
    if ok:
        await audit.log(
            "memory_write", member.id, "memory.change_scope",
            resource_type="memory", resource_id=memory_id,
            payload={"scope": scope, "scope_id": scope_id},
        )
    return ok


# --------------------------------------------------------------------------- #
# Merge & conflict / staleness
# --------------------------------------------------------------------------- #
def _tokens(content: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (content or "").lower()) if len(t) > 2}


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


async def merge_memories(member: Member, *, primary_id: str, duplicate_ids: list[str]) -> int:
    """Mark each duplicate as superseded by ``primary_id``.

    Superseded entries are excluded from retrieval but remain visible (with a
    'merged into' pointer) in the control center. Returns the count superseded.
    """
    table = await reflect_table("memory_entries")
    org = member.organization_id
    targets = [d for d in duplicate_ids if d != primary_id]
    if not targets:
        return 0
    async with engine.begin() as conn:
        primary = (
            await conn.execute(
                select(table.c.id).where(
                    table.c.id == primary_id,
                    table.c.organization_id == org,
                    table.c.is_deleted.is_(False),
                )
            )
        ).first()
        if primary is None:
            return 0
        result = await conn.execute(
            update(table)
            .where(
                table.c.id.in_(targets),
                table.c.organization_id == org,
                table.c.is_deleted.is_(False),
            )
            .values(superseded_by=primary_id, updated_at=func.now())
            .returning(table.c.id)
        )
        superseded = result.scalars().all()
    await audit.log(
        "memory_write", member.id, "memory.merge",
        resource_type="memory", resource_id=primary_id,
        payload={"superseded": [str(s) for s in superseded]},
    )
    return len(superseded)


async def _active_rows(org_id: str) -> list[dict[str, Any]]:
    table = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    table.c.id, table.c.scope, table.c.scope_id, table.c.content,
                    table.c.created_at, table.c.importance_score,
                ).where(
                    table.c.organization_id == org_id,
                    table.c.is_deleted.is_(False),
                    table.c.is_archived.is_(False),
                    table.c.superseded_by.is_(None),
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def detect_conflicts(member: Member) -> list[dict[str, Any]]:
    """Find near-duplicate active memories within the same scope.

    Deterministic (token overlap). For each conflicting pair the older entry is
    proposed as the stale one to supersede with the newer survivor.
    """
    rows = await _active_rows(member.organization_id)
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault((r["scope"], r["scope_id"]), []).append(r)

    conflicts: list[dict[str, Any]] = []
    for group in buckets.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                sim = _similarity(a["content"], b["content"])
                if sim < CONFLICT_SIMILARITY_THRESHOLD:
                    continue
                older, newer = sorted(
                    (a, b), key=lambda r: (r["created_at"] is not None, r["created_at"])
                )
                conflicts.append(
                    {
                        "stale_id": str(older["id"]),
                        "survivor_id": str(newer["id"]),
                        "similarity": round(sim, 3),
                        "scope": older["scope"],
                        "stale_content": older["content"],
                        "survivor_content": newer["content"],
                    }
                )
    conflicts.sort(key=lambda c: c["similarity"], reverse=True)
    return conflicts


async def resolve_conflict(member: Member, *, stale_id: str, survivor_id: str) -> bool:
    """Supersede a stale memory with its survivor (drops it from retrieval)."""
    if stale_id == survivor_id:
        return False
    ok = await _set_fields(stale_id, member.organization_id, superseded_by=survivor_id)
    if ok:
        await audit.log(
            "memory_write", member.id, "memory.resolve_conflict",
            resource_type="memory", resource_id=stale_id,
            payload={"survivor": survivor_id},
        )
    return ok


# --------------------------------------------------------------------------- #
# Listing, search, import/export
# --------------------------------------------------------------------------- #
async def list_memories(
    member: Member,
    *,
    query: str | None = None,
    scope: str | None = None,
    include_archived: bool = False,
    include_superseded: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Control-center listing with flags + optional text search / filters."""
    table = await reflect_table("memory_entries")
    conditions = [
        table.c.organization_id == member.organization_id,
        table.c.is_deleted.is_(False),
    ]
    if not include_archived:
        conditions.append(table.c.is_archived.is_(False))
    if not include_superseded:
        conditions.append(table.c.superseded_by.is_(None))
    if scope:
        conditions.append(table.c.scope == scope)
    if query:
        conditions.append(table.c.content.ilike(f"%{query}%"))
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    table.c.id, table.c.scope, table.c.scope_id, table.c.content,
                    table.c.source, table.c.importance_score, table.c.is_archived,
                    table.c.is_pinned, table.c.is_sensitive, table.c.superseded_by,
                    table.c.created_by, table.c.created_at, table.c.updated_at,
                )
                .where(*conditions)
                .order_by(table.c.is_pinned.desc(), table.c.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def export_memories(member: Member) -> list[dict[str, Any]]:
    """Portable JSON export of an org's active memories."""
    rows = await list_memories(member, include_archived=True, include_superseded=False, limit=10000)
    fields = ("content", "scope", "scope_id", "source", "importance_score",
              "is_pinned", "is_sensitive")
    export = [{k: r.get(k) for k in fields} for r in rows]
    await audit.log(
        "memory_export", member.id, "memory.export",
        resource_type="memory", payload={"count": len(export)},
    )
    return export


async def import_memories(member: Member, items: list[dict[str, Any]]) -> list[str]:
    """Create memory entries from a prior export (round-trip)."""
    from core.memory_writes import create_memory_entry

    ctx = RequesterContext.from_member(member)
    ids: list[str] = []
    for item in items:
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        scope = str(item.get("scope") or "org")
        scope_id = str(item.get("scope_id") or member.organization_id)
        entry_id = await create_memory_entry(
            content=content,
            requester_context=ctx,
            source="imported",
            scope=scope,
            scope_id=scope_id,
            importance_score=float(item.get("importance_score") or 0.7),
            created_by=member.id,
        )
        if item.get("is_pinned"):
            await _set_fields(entry_id, member.organization_id, is_pinned=True)
        if item.get("is_sensitive"):
            await _set_fields(entry_id, member.organization_id, is_sensitive=True)
        ids.append(entry_id)
    await audit.log(
        "memory_import", member.id, "memory.import",
        resource_type="memory", payload={"count": len(ids)},
    )
    return ids
