from __future__ import annotations
from datetime import datetime, timezone
import json
import re
from typing import Any

from sqlalchemy import select, text, update

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.embeddings import embed
from core.llm import complete_json
from core.memory_writes import EXPECTED_EMBEDDING_DIMENSIONS
from core.models import MemoryEntry, RequesterContext


async def _retrieve_recent_memories(
    requester_context: RequesterContext,
    *,
    decision: str,
) -> list[MemoryEntry]:
    scope_filter, scope_params = _authorized_scope_sql(requester_context)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    f"""
                    SELECT id, organization_id, region, scope, scope_id, content, source,
                           importance_score, is_deleted, created_by, created_at
                    FROM memory_entries
                    WHERE organization_id = :org_id
                      AND is_deleted = FALSE
                      AND ({scope_filter})
                    ORDER BY created_at DESC
                    LIMIT 10
                    """
                ),
                {"org_id": requester_context.org_id, **scope_params},
            )
        ).mappings().all()
    await audit.log(
        "memory_retrieve",
        requester_context.member_id,
        "memory.retrieve",
        resource_type="memory_entries",
        payload={"fallback": "recent_memory"},
        decision=decision,
    )
    return [MemoryEntry(**_memory_entry_payload(dict(row))) for row in rows]


def _authorized_scope_pairs(requester_context: RequesterContext) -> list[tuple[str, str]]:
    pairs = [("org", requester_context.org_id)]
    if requester_context.workspace_id:
        pairs.append(("workspace", requester_context.workspace_id))
    if requester_context.persona_id:
        pairs.append(("persona", requester_context.persona_id))
    if requester_context.member_id:
        pairs.append(("personal", requester_context.member_id))
        pairs.append(("restricted", requester_context.member_id))
    if requester_context.role in {"owner", "admin"}:
        pairs.append(("restricted", requester_context.org_id))
    return pairs


async def expand_query(query: str, requester_context: RequesterContext) -> list[str]:
    """Generate recall-oriented memory search variants.

    The original query is always first. If the model path fails, retrieval keeps
    working with a small deterministic expansion so memory is not provider-bound.
    """
    variants = [query.strip()]
    if not variants[0]:
        return []
    prompt = f"""
Generate 3 alternative search queries for enterprise memory retrieval.
Focus on durable facts that would help answer or execute the request.
Return JSON only: {{"queries": ["...", "...", "..."]}}

Retrieval context: {requester_context.memory_context}
Task id present: {bool(requester_context.task_id)}
Query: {query}
"""
    try:
        parsed = json.loads(await complete_json(prompt, model=settings.fast_model))
        candidates = parsed.get("queries") if isinstance(parsed, dict) else []
    except Exception:
        candidates = _fallback_query_variants(query)

    for candidate in candidates:
        text_value = str(candidate).strip()
        if text_value and text_value.lower() not in {item.lower() for item in variants}:
            variants.append(text_value)
        if len(variants) >= 4:
            break
    return variants


def _fallback_query_variants(query: str) -> list[str]:
    normalized = " ".join(query.split())
    variants = []
    if normalized:
        variants.append(normalized)
    words = [word for word in re.findall(r"[a-zA-Z0-9']+", normalized.lower()) if len(word) > 3]
    if words:
        variants.append(" ".join(words[:8]))
    if any(term in normalized.lower() for term in ("follow up", "call", "email", "client", "customer")):
        variants.append("contact preference relationship notes")
    return variants


def _authorized_scope_sql(requester_context: RequesterContext) -> tuple[str, dict[str, str]]:
    clauses = []
    params: dict[str, str] = {}
    for index, (scope, scope_id) in enumerate(_authorized_scope_pairs(requester_context)):
        scope_key = f"scope_{index}"
        scope_id_key = f"scope_id_{index}"
        clauses.append(f"(scope = :{scope_key} AND scope_id = :{scope_id_key})")
        params[scope_key] = scope
        params[scope_id_key] = scope_id
    return " OR ".join(clauses) or "FALSE", params


def _recency_score(created_at: Any, *, now: datetime | None = None) -> float:
    if created_at is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = max((now - created_at).total_seconds() / 86400, 0.0)
    return 1.0 / (1.0 + age_days / 30.0)


def _source_authority(source: str | None) -> float:
    return {
        "explicit": 1.0,
        "scratchpad": 0.95,
        "autonomous": 0.7,
        "synthesized": 0.55,
    }.get(str(source or "").lower(), 0.5)


def _rank_memory_rows(rows: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    ranked = []
    for row in rows:
        distance = float(row.get("distance") or 1.0)
        cosine = max(0.0, min(1.0, 1.0 - distance))
        importance = max(0.0, min(float(row.get("importance_score") or 0.0), 1.0))
        recency = _recency_score(row.get("created_at"), now=now)
        authority = _source_authority(row.get("source"))
        ranked.append(
            {
                **row,
                "_rank_score": (0.45 * cosine) + (0.20 * recency) + (0.20 * importance) + (0.15 * authority),
            }
        )
    return sorted(ranked, key=lambda row: row["_rank_score"], reverse=True)


def _dedupe_ranked_rows(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    final: list[dict[str, Any]] = []
    fingerprints: list[set[str]] = []
    for row in rows:
        tokens = _content_tokens(str(row.get("content") or ""))
        duplicate_index = next(
            (index for index, seen in enumerate(fingerprints) if _jaccard(tokens, seen) >= 0.82),
            None,
        )
        if duplicate_index is not None:
            existing = final[duplicate_index]
            if float(row.get("_rank_score") or 0.0) > float(existing.get("_rank_score") or 0.0):
                final[duplicate_index] = row
                fingerprints[duplicate_index] = tokens
            continue
        final.append(row)
        fingerprints.append(tokens)
        if len(final) >= limit:
            break
    return final


def _content_tokens(content: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", content.lower()) if token}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _memory_entry_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key in MemoryEntry.model_fields}


async def _retrieve_task_scratchpad(requester_context: RequesterContext) -> list[MemoryEntry]:
    if not requester_context.task_id:
        return []
    try:
        tasks = await reflect_table("tasks")
    except Exception:
        return []
    async with engine.begin() as conn:
        row = (
            await conn.execute(select(tasks.c.agent_state).where(tasks.c.id == requester_context.task_id))
        ).mappings().first()
    if not row:
        return []
    state = row.get("agent_state") or {}
    entries = state.get("memory_scratchpad") if isinstance(state, dict) else []
    if not isinstance(entries, list):
        return []
    memories: list[MemoryEntry] = []
    for index, entry in enumerate(entries[-10:]):
        if not isinstance(entry, dict) or not str(entry.get("content") or "").strip():
            continue
        memories.append(
            MemoryEntry(
                id=str(entry.get("id") or f"scratch-{requester_context.task_id}-{index}"),
                organization_id=requester_context.org_id,
                region=str(entry.get("region") or "us"),
                content=str(entry["content"]),
                scope="task",
                scope_id=requester_context.task_id,
                source="scratchpad",
                importance_score=max(0.0, min(float(entry.get("importance_score") or 1.0), 1.0)),
                created_by=str(entry.get("created_by") or requester_context.member_id),
                created_at=entry.get("created_at"),
            )
        )
    return memories


async def add_task_scratchpad_memory(
    task_id: str,
    content: str,
    *,
    requester_context: RequesterContext,
    importance_score: float = 1.0,
) -> None:
    """Store task-local working memory in task.agent_state.

    Scratchpad memory is intentionally ephemeral to the task and is retrieved
    before long-term memory when RequesterContext.task_id is set.
    """
    tasks = await reflect_table("tasks")
    now = datetime.now(timezone.utc).isoformat()
    async with engine.begin() as conn:
        row = (await conn.execute(select(tasks.c.agent_state).where(tasks.c.id == task_id))).mappings().first()
        state = dict(row.get("agent_state") or {}) if row else {}
        scratchpad = list(state.get("memory_scratchpad") or [])
        scratchpad.append(
            {
                "id": f"scratch-{task_id}-{len(scratchpad)}",
                "content": content,
                "importance_score": max(0.0, min(float(importance_score), 1.0)),
                "created_by": requester_context.member_id,
                "created_at": now,
            }
        )
        state["memory_scratchpad"] = scratchpad[-50:]
        await conn.execute(update(tasks).where(tasks.c.id == task_id).values(agent_state=state))
    await audit.log(
        "memory_scratchpad_write",
        requester_context.member_id,
        "memory.scratchpad",
        resource_type="tasks",
        resource_id=task_id,
        payload={"content_preview": content[:120]},
    )


async def retrieve(query: str, requester_context: RequesterContext) -> list[MemoryEntry]:
    expanded_queries = await expand_query(query, requester_context)
    scratchpad = await _retrieve_task_scratchpad(requester_context)
    embeddings: list[tuple[str, list[float]]] = []
    try:
        for expanded_query in expanded_queries:
            embeddings.append((expanded_query, await embed(expanded_query)))
    except Exception as exc:
        await audit.log(
            "memory_retrieve_error",
            requester_context.member_id,
            "memory.retrieve",
            resource_type="memory_entries",
            payload={"query_preview": query[:120], "error": str(exc)[:240]},
            decision="embedding_failed",
        )
        recent = await _retrieve_recent_memories(requester_context, decision="embedding_failed")
        return (scratchpad + recent)[:10]
    bad_embedding = next((vector for _, vector in embeddings if len(vector) != EXPECTED_EMBEDDING_DIMENSIONS), None)
    if bad_embedding is not None:
        await audit.log(
            "memory_retrieve_error",
            requester_context.member_id,
            "memory.retrieve",
            resource_type="memory_entries",
            payload={
                "query_preview": query[:120],
                "expected_dimensions": EXPECTED_EMBEDDING_DIMENSIONS,
                "actual_dimensions": len(bad_embedding),
            },
            decision="dimension_mismatch",
        )
        recent = await _retrieve_recent_memories(requester_context, decision="dimension_mismatch")
        return (scratchpad + recent)[:10]
    scope_filter, scope_params = _authorized_scope_sql(requester_context)
    rows_by_id: dict[str, dict[str, Any]] = {}
    async with engine.begin() as conn:
        for expanded_query, query_embedding in embeddings:
            vector_literal = "[" + ",".join(str(value) for value in query_embedding) + "]"
            rows = (
                await conn.execute(
                    text(
                        f"""
                        SELECT id, organization_id, region, scope, scope_id, content, source,
                               importance_score, is_deleted, created_by, created_at,
                               embedding <=> (:embedding)::vector AS distance
                        FROM memory_entries
                        WHERE organization_id = :org_id
                          AND is_deleted = FALSE
                          AND embedding IS NOT NULL
                          AND ({scope_filter})
                        ORDER BY embedding <=> (:embedding)::vector
                        LIMIT 30
                        """
                    ),
                    {"org_id": requester_context.org_id, "embedding": vector_literal, **scope_params},
                )
            ).mappings().all()
            for row in rows:
                row_dict = dict(row)
                row_dict["matched_query"] = expanded_query
                existing = rows_by_id.get(str(row_dict["id"]))
                if existing is None or float(row_dict.get("distance") or 1.0) < float(existing.get("distance") or 1.0):
                    rows_by_id[str(row_dict["id"])] = row_dict
    ranked = _dedupe_ranked_rows(_rank_memory_rows(list(rows_by_id.values())), limit=max(10 - len(scratchpad), 0))
    await audit.log(
        "memory_retrieve",
        requester_context.member_id,
        "memory.retrieve",
        resource_type="memory_entries",
        payload={
            "query_preview": query[:120],
            "expanded_query_count": len(expanded_queries),
            "scratchpad_count": len(scratchpad),
            "authorized_scopes": _authorized_scope_pairs(requester_context),
        },
        decision="expanded_scoped_memory_search",
    )
    return (scratchpad + [MemoryEntry(**_memory_entry_payload(row)) for row in ranked])[:10]
