from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from core import audit
from core.db import engine
from core.embeddings import embed
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


def _rank_memory_rows(rows: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    ranked = []
    for row in rows:
        distance = float(row.get("distance") or 1.0)
        cosine = max(0.0, min(1.0, 1.0 - distance))
        importance = max(0.0, min(float(row.get("importance_score") or 0.0), 1.0))
        recency = _recency_score(row.get("created_at"), now=now)
        ranked.append({**row, "_rank_score": (0.60 * cosine) + (0.25 * importance) + (0.15 * recency)})
    return sorted(ranked, key=lambda row: row["_rank_score"], reverse=True)


def _memory_entry_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key in MemoryEntry.model_fields}


async def retrieve(query: str, requester_context: RequesterContext) -> list[MemoryEntry]:
    try:
        query_embedding = await embed(query)
    except Exception as exc:
        await audit.log(
            "memory_retrieve_error",
            requester_context.member_id,
            "memory.retrieve",
            resource_type="memory_entries",
            payload={"query_preview": query[:120], "error": str(exc)[:240]},
            decision="embedding_failed",
        )
        return await _retrieve_recent_memories(requester_context, decision="embedding_failed")
    if len(query_embedding) != EXPECTED_EMBEDDING_DIMENSIONS:
        await audit.log(
            "memory_retrieve_error",
            requester_context.member_id,
            "memory.retrieve",
            resource_type="memory_entries",
            payload={
                "query_preview": query[:120],
                "expected_dimensions": EXPECTED_EMBEDDING_DIMENSIONS,
                "actual_dimensions": len(query_embedding),
            },
            decision="dimension_mismatch",
        )
        return await _retrieve_recent_memories(requester_context, decision="dimension_mismatch")
    vector_literal = "[" + ",".join(str(value) for value in query_embedding) + "]"
    scope_filter, scope_params = _authorized_scope_sql(requester_context)
    async with engine.begin() as conn:
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
                    LIMIT 40
                    """
                ),
                {"org_id": requester_context.org_id, "embedding": vector_literal, **scope_params},
            )
        ).mappings().all()
    ranked = _rank_memory_rows([dict(row) for row in rows])[:10]
    await audit.log(
        "memory_retrieve",
        requester_context.member_id,
        "memory.retrieve",
        resource_type="memory_entries",
        payload={"query_preview": query[:120], "authorized_scopes": _authorized_scope_pairs(requester_context)},
        decision="scoped_hybrid_search",
    )
    return [MemoryEntry(**_memory_entry_payload(row)) for row in ranked]
