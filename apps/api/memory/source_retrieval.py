"""Task 10 — permission-aware source retrieval + citations.

Vector-searches project_source_chunks (org + project scoped) for the chunks most
relevant to a query, but only for callers who are members of the project. A
non-member gets nothing. Embedding failures return no citations rather than
fabricating any — every Citation carries a backing snippet.

Mirrors the vector-search pattern in core/memory.py (raw text() query, embedding
passed as a "[v1,v2,...]" literal string).
"""
from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import text

from core import audit
from core.db import engine, reflect_table
from core.embeddings import embed
from core.memory_writes import EXPECTED_EMBEDDING_DIMENSIONS
from core.models import RequesterContext

_SNIPPET_CHARS = 600


class Citation(BaseModel):
    source_id: str
    source_title: str | None = None
    chunk_index: int
    snippet: str
    distance: float | None = None


async def _is_project_member(project_id: str, member_id: str, org_id: str) -> bool:
    """True iff a project_members row matches (project_id, member_id, org_id)."""
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                project_members.select().where(
                    project_members.c.project_id == project_id,
                    project_members.c.member_id == member_id,
                    project_members.c.organization_id == org_id,
                )
            )
        ).first()
    return row is not None


async def retrieve_source_chunks(
    query: str,
    requester_context: RequesterContext,
    *,
    limit: int = 6,
) -> list[Citation]:
    """Return the most relevant project source chunks as Citations.

    Returns an empty list when there is no project, the caller is not a project
    member, or the query embedding fails / has the wrong dimension.
    """
    project_id = requester_context.project_id
    if project_id is None:
        return []

    # Permission-aware: a non-member gets nothing.
    if not await _is_project_member(project_id, requester_context.member_id, requester_context.org_id):
        await audit.log(
            "source_retrieve_denied",
            requester_context.member_id,
            "source.retrieve",
            organization_id=requester_context.org_id,
            resource_type="project_source_chunks",
            resource_id=project_id,
            payload={"project_id": project_id, "query_preview": query[:120]},
            decision="not_a_member",
        )
        return []

    # Embedding — honest on failure: no citations, never fabricated.
    try:
        query_embedding = await embed(query)
    except Exception:
        await audit.log(
            "source_retrieve_error",
            requester_context.member_id,
            "source.retrieve",
            organization_id=requester_context.org_id,
            resource_type="project_source_chunks",
            resource_id=project_id,
            payload={"project_id": project_id, "query_preview": query[:120]},
            decision="embedding_error",
        )
        return []
    if len(query_embedding) != EXPECTED_EMBEDDING_DIMENSIONS:
        await audit.log(
            "source_retrieve_error",
            requester_context.member_id,
            "source.retrieve",
            organization_id=requester_context.org_id,
            resource_type="project_source_chunks",
            resource_id=project_id,
            payload={
                "project_id": project_id,
                "query_preview": query[:120],
                "expected_dimensions": EXPECTED_EMBEDDING_DIMENSIONS,
                "actual_dimensions": len(query_embedding),
            },
            decision="dimension_mismatch",
        )
        return []

    vector_literal = "[" + ",".join(str(value) for value in query_embedding) + "]"
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT c.source_id, c.chunk_index, c.content, s.title AS source_title,
                           c.embedding <=> (:embedding)::vector AS distance
                    FROM project_source_chunks c
                    JOIN project_sources s ON s.id = c.source_id
                    WHERE c.organization_id = :org_id
                      AND c.project_id = :project_id
                      AND c.embedding IS NOT NULL
                    ORDER BY c.embedding <=> (:embedding)::vector
                    LIMIT :limit
                    """
                ),
                {
                    "org_id": requester_context.org_id,
                    "project_id": project_id,
                    "embedding": vector_literal,
                    "limit": limit,
                },
            )
        ).mappings().all()

    citations = [
        Citation(
            source_id=str(row["source_id"]),
            source_title=row.get("source_title"),
            chunk_index=int(row["chunk_index"]),
            snippet=str(row["content"])[:_SNIPPET_CHARS],
            distance=float(row["distance"]) if row.get("distance") is not None else None,
        )
        for row in rows
    ]

    await audit.log(
        "source_retrieve",
        requester_context.member_id,
        "source.retrieve",
        organization_id=requester_context.org_id,
        resource_type="project_source_chunks",
        resource_id=project_id,
        payload={
            "project_id": project_id,
            "query_preview": query[:120],
            "result_count": len(citations),
        },
        decision="scoped_source_search",
    )
    return citations


def build_knowledge_block(citations: list[Citation]) -> str:
    """Render a ``# Project Knowledge`` markdown block with stable [S#] markers.

    Returns "" for an empty list.
    """
    if not citations:
        return ""
    lines = [
        "# Project Knowledge",
        "Cite these sources inline using their [S#] marker when you use them.",
        "",
    ]
    for i, citation in enumerate(citations, start=1):
        title = citation.source_title or "Untitled source"
        lines.append(f"[S{i}] {title}")
        lines.append(citation.snippet)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def citations_payload(citations: list[Citation]) -> list[dict]:
    """Turn citations into the JSON list persisted on a message.

    Every entry carries its backing snippet — no citation without a snippet.
    """
    return [
        {
            "marker": f"S{i}",
            "source_id": citation.source_id,
            "source_title": citation.source_title,
            "chunk_index": citation.chunk_index,
            "snippet": citation.snippet,
        }
        for i, citation in enumerate(citations, start=1)
    ]
