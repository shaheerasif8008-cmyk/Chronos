from sqlalchemy import text

from core import audit
from core.db import engine
from core.embeddings import embed
from core.models import MemoryEntry, RequesterContext


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
        return []
    vector_literal = "[" + ",".join(str(value) for value in query_embedding) + "]"
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT id, organization_id, region, scope, scope_id, content, source,
                           importance_score, is_deleted, created_by
                    FROM memory_entries
                    WHERE organization_id = :org_id
                      AND is_deleted = FALSE
                    ORDER BY embedding <=> (:embedding)::vector
                    LIMIT 10
                    """
                ),
                {"org_id": requester_context.org_id, "embedding": vector_literal},
            )
        ).mappings().all()
    await audit.log(
        "memory_retrieve",
        requester_context.member_id,
        "memory.retrieve",
        resource_type="memory_entries",
        payload={"query_preview": query[:120]},
        decision="unfiltered_vector_search",
    )
    return [MemoryEntry(**dict(row)) for row in rows]
