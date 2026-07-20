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

import math
import re
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.sql import func

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.memory_access import (
    get_memory_for_member,
    memory_access_condition,
    normalize_entry_scope,
    validate_policy_target,
)
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


async def get_memory_policy(
    member: Member,
    *,
    scope: str,
    scope_id: str | None,
) -> dict[str, Any]:
    """Return the canonical policy target and its effective capture state."""
    canonical_scope, canonical_id = await validate_policy_target(member, scope, scope_id)
    explicit_enabled = not await _policy_disabled(
        str(member.organization_id), canonical_scope, canonical_id
    )
    effective_enabled = await is_memory_enabled(
        org_id=str(member.organization_id),
        project_id=canonical_id if canonical_scope == "project" else None,
        member_id=canonical_id if canonical_scope == "member" else None,
        conversation_id=canonical_id if canonical_scope == "conversation" else None,
    )
    return {
        "scope": canonical_scope,
        "scope_id": canonical_id,
        "enabled": explicit_enabled,
        "effective_enabled": effective_enabled,
    }


async def set_memory_policy(
    member: Member,
    *,
    scope: str,
    scope_id: str | None,
    enabled: bool,
) -> dict[str, Any]:
    """Enable/disable memory for a scope (org|project|member|conversation)."""
    from core.settings_store import save_settings_doc

    scope, scope_id = await validate_policy_target(member, scope, scope_id)
    doc = await save_settings_doc(
        member, "memory_policy", {"enabled": enabled}, scope=scope, scope_id=scope_id
    )
    await audit.log(
        "memory_policy",
        member.id,
        "memory.policy.set",
        organization_id=member.organization_id,
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
            "conversation_id": requester_context.conversation_id,
            "task_id": requester_context.task_id,
            "used_by": requester_context.member_id,
            "context": context,
        }
        for mid in memory_ids
    ]
    async with engine.begin() as conn:
        await conn.execute(insert(table), rows)


async def list_memory_usage(memory_id: str, member: Member, *, limit: int = 50) -> list[dict[str, Any]]:
    if await get_memory_for_member(memory_id, member) is None:
        return []
    table = await reflect_table("memory_usage_log")
    conditions = [
        table.c.memory_id == memory_id,
        table.c.organization_id == member.organization_id,
    ]
    if member.role not in {"admin", "owner"}:
        conditions.append(table.c.used_by == member.id)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(table)
                .where(*conditions)
                .order_by(table.c.created_at.desc())
                .limit(limit)
            )
        ).mappings().all()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Control-center flag updates
# --------------------------------------------------------------------------- #
async def _set_fields(memory_id: str, member: Member, **values: Any) -> bool:
    memory = await get_memory_for_member(memory_id, member, mutate=True)
    if memory is None:
        return False
    table = await reflect_table("memory_entries")
    values["updated_at"] = func.now()
    async with engine.begin() as conn:
        result = await conn.execute(
            update(table)
            .where(
                table.c.id == memory_id,
                table.c.organization_id == member.organization_id,
                table.c.scope == memory["scope"],
                table.c.scope_id == memory["scope_id"],
                table.c.is_deleted.is_(False),
            )
            .values(**values)
            .returning(table.c.id)
        )
        return result.scalar_one_or_none() is not None


async def archive_memory(memory_id: str, member: Member, *, archived: bool = True) -> bool:
    ok = await _set_fields(memory_id, member, is_archived=archived)
    if ok:
        await audit.log(
            "memory_write", member.id, "memory.archive" if archived else "memory.unarchive",
            organization_id=member.organization_id,
            resource_type="memory", resource_id=memory_id,
        )
    return ok


async def set_pinned(memory_id: str, member: Member, *, pinned: bool) -> bool:
    ok = await _set_fields(memory_id, member, is_pinned=pinned)
    if ok:
        await audit.log(
            "memory_write", member.id, "memory.pin" if pinned else "memory.unpin",
            organization_id=member.organization_id,
            resource_type="memory", resource_id=memory_id,
        )
    return ok


async def set_sensitive(memory_id: str, member: Member, *, sensitive: bool) -> bool:
    ok = await _set_fields(memory_id, member, is_sensitive=sensitive)
    if ok:
        await audit.log(
            "memory_write", member.id, "memory.classify_sensitive",
            organization_id=member.organization_id,
            resource_type="memory", resource_id=memory_id,
            payload={"sensitive": sensitive},
        )
    return ok


async def change_scope(memory_id: str, member: Member, *, scope: str, scope_id: str) -> bool:
    current = await get_memory_for_member(memory_id, member, mutate=True)
    if current is None:
        return False
    try:
        scope, scope_id = await normalize_entry_scope(member, scope, scope_id)
    except ValueError:
        return False
    ok = await _set_fields(memory_id, member, scope=scope, scope_id=scope_id)
    if ok:
        await audit.log(
            "memory_write", member.id, "memory.change_scope",
            organization_id=member.organization_id,
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
    primary = await get_memory_for_member(primary_id, member, mutate=True)
    duplicates = [await get_memory_for_member(item, member, mutate=True) for item in targets]
    if primary is None or any(item is None for item in duplicates):
        return 0
    scope_pair = (str(primary["scope"]), str(primary["scope_id"]))
    if any((str(item["scope"]), str(item["scope_id"])) != scope_pair for item in duplicates if item):
        # Cross-scope supersession can make a private row point at data its
        # reader cannot access, so merges are deliberately scope-local.
        return 0
    async with engine.begin() as conn:
        result = await conn.execute(
            update(table)
            .where(
                table.c.id.in_(targets),
                table.c.organization_id == org,
                table.c.scope == scope_pair[0],
                table.c.scope_id == scope_pair[1],
                table.c.is_deleted.is_(False),
            )
            .values(superseded_by=primary_id, updated_at=func.now())
            .returning(table.c.id)
        )
        superseded = result.scalars().all()
    await audit.log(
        "memory_write", member.id, "memory.merge",
            organization_id=member.organization_id,
        resource_type="memory", resource_id=primary_id,
        payload={"superseded": [str(s) for s in superseded]},
    )
    return len(superseded)


async def _active_rows(member: Member) -> list[dict[str, Any]]:
    table = await reflect_table("memory_entries")
    visible = await memory_access_condition(table, member)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    table.c.id, table.c.scope, table.c.scope_id, table.c.content,
                    table.c.created_at, table.c.importance_score,
                ).where(
                    table.c.organization_id == member.organization_id,
                    table.c.is_deleted.is_(False),
                    table.c.is_archived.is_(False),
                    table.c.superseded_by.is_(None),
                    visible,
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def detect_conflicts(member: Member) -> list[dict[str, Any]]:
    """Find near-duplicate active memories within the same scope.

    Deterministic (token overlap). For each conflicting pair the older entry is
    proposed as the stale one to supersede with the newer survivor.
    """
    rows = await _active_rows(member)
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
    stale = await get_memory_for_member(stale_id, member, mutate=True)
    survivor = await get_memory_for_member(survivor_id, member, mutate=True)
    if stale is None or survivor is None:
        return False
    if (stale["scope"], stale["scope_id"]) != (survivor["scope"], survivor["scope_id"]):
        return False
    ok = await _set_fields(stale_id, member, superseded_by=survivor_id)
    if ok:
        await audit.log(
            "memory_write", member.id, "memory.resolve_conflict",
            organization_id=member.organization_id,
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
        await memory_access_condition(table, member),
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
    """Portable JSON export of an org's non-deleted memories.

    Archived entries are included and tagged so a user-visible organization
    export does not silently drop memories the control center can still show.
    Superseded entries remain excluded because they are retained as history, not
    current memory state.
    """
    rows = await list_memories(member, include_archived=True, include_superseded=False, limit=10000)
    fields = ("content", "scope", "scope_id", "source", "importance_score",
              "is_archived", "is_pinned", "is_sensitive")
    export = [{k: r.get(k) for k in fields} for r in rows]
    await audit.log(
        "memory_export", member.id, "memory.export",
            organization_id=member.organization_id,
        resource_type="memory", payload={"count": len(export)},
    )
    return export


async def import_memories(member: Member, items: list[dict[str, Any]]) -> list[str]:
    """Create memory entries from a prior export (round-trip)."""
    from core.memory_writes import create_memory_entry

    if len(items) > 10_000:
        raise ValueError("memory import is limited to 10000 entries")
    ctx = RequesterContext.from_member(member)
    normalized: list[tuple[str, str, str, float, bool, bool, bool]] = []
    for index, item in enumerate(items):
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 10_000:
            raise ValueError(f"memory import item {index} exceeds 10000 characters")
        scope = str(item.get("scope") or "org")
        scope_id = item.get("scope_id")
        if scope in {"org", "workspace", "personal", "restricted"}:
            # Portable exports carry source-tenant ids. Canonical scopes are
            # intentionally rebound to the importing tenant/member.
            scope_id = None
        normalized_scope, normalized_scope_id = await normalize_entry_scope(
            member, scope, str(scope_id) if scope_id is not None else None
        )
        try:
            importance = float(item.get("importance_score", 0.7))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"memory import item {index} has invalid importance_score") from exc
        if not math.isfinite(importance) or not 0.0 <= importance <= 1.0:
            raise ValueError(f"memory import item {index} has invalid importance_score")
        flags: list[bool] = []
        for field in ("is_pinned", "is_archived", "is_sensitive"):
            value = item.get(field, False)
            if not isinstance(value, bool):
                raise ValueError(f"memory import item {index} has invalid {field}")
            flags.append(value)
        normalized.append(
            (
                content,
                normalized_scope,
                normalized_scope_id,
                importance,
                flags[0],
                flags[1],
                flags[2],
            )
        )

    # Validate every target before the first write so a malformed import cannot
    # leave a partially imported memory set.
    ids: list[str] = []
    for content, scope, scope_id, importance, pinned, archived, sensitive in normalized:
        entry_id = await create_memory_entry(
            content=content,
            requester_context=ctx,
            source="imported",
            scope=scope,
            scope_id=scope_id,
            importance_score=importance,
            created_by=member.id,
        )
        if pinned:
            await _set_fields(entry_id, member, is_pinned=True)
        if archived:
            await _set_fields(entry_id, member, is_archived=True)
        if sensitive:
            await _set_fields(entry_id, member, is_sensitive=True)
        ids.append(entry_id)
    await audit.log(
        "memory_import", member.id, "memory.import",
            organization_id=member.organization_id,
        resource_type="memory", payload={"count": len(ids)},
    )
    return ids
