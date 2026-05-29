"""Source indexing pipeline — chunk + embed a project source into project_source_chunks.

Pure pipeline + DB (no FastAPI/router concerns), mirroring the other memory/ modules.
Every query is org-scoped. Embeddings go through core.embeddings.embed; vectors are
written via core.memory_writes.vector_literal exactly like create_memory_entry does.
"""
from __future__ import annotations

from sqlalchemy import delete, insert, select, update
from sqlalchemy.sql import func

from core import audit
from core.artifacts import (
    get_artifact,
    read_artifact_content,
    save_artifact,
    set_parse_status,
)
from core.config import settings
from core.db import engine, reflect_table
from core.embeddings import embed
from core.memory_writes import EXPECTED_EMBEDDING_DIMENSIONS, vector_literal

# ~800-token chunks with ~100-token overlap, approximated at 4 chars/token.
CHUNK_CHARS = 3200
CHUNK_OVERLAP_CHARS = 400


def _chunk_text(text: str) -> list[str]:
    """Split text into ~CHUNK_CHARS windows with CHUNK_OVERLAP_CHARS overlap."""
    if not text:
        return []
    step = CHUNK_CHARS - CHUNK_OVERLAP_CHARS
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunk = text[start : start + CHUNK_CHARS]
        if chunk:
            chunks.append(chunk)
        if start + CHUNK_CHARS >= len(text):
            break
        start += step
    return chunks


async def _resolve_source_text(source: dict, org_id: str) -> tuple[str, bool]:
    """Return (full_text, parse_failed) for a source, parsing its artifact on demand.

    Reuses the parsed_text child artifact when present; otherwise parses the source's
    attachment via parsing.engine.parse_document and caches the result, mirroring the
    parse-on-send flow in routers/chat.py.
    """
    artifact_id = source.get("artifact_id")
    if not artifact_id:
        return "", False
    artifact_id = str(artifact_id)

    meta = await get_artifact(artifact_id)
    if not meta or str(meta.get("organization_id")) != str(org_id):
        return "", False

    artifacts = await reflect_table("artifacts")

    # Cache hit: reuse the stored parsed_text child rather than re-parsing.
    if str(meta.get("parse_status")) == "parsed":
        async with engine.begin() as conn:
            child = (
                await conn.execute(
                    select(artifacts).where(
                        artifacts.c.parent_artifact_id == artifact_id,
                        artifacts.c.kind == "parsed_text",
                    )
                )
            ).mappings().first()
        if child:
            full = (await read_artifact_content(str(child["id"])) or b"").decode(
                "utf-8", errors="replace"
            )
            return full, False

    # Parse on demand.
    from parsing.engine import UNPARSEABLE_NOTE, parse_document

    raw = await read_artifact_content(artifact_id) or b""
    doc = await parse_document(raw, str(meta.get("mime_type") or ""), str(meta.get("title") or "file"))
    if doc.parser_used != "none":
        status = "parsed"
    elif doc.note == UNPARSEABLE_NOTE:
        status = "unparseable"
    else:
        status = "failed"

    if doc.full_text:
        await save_artifact(
            doc.full_text,
            kind="parsed_text",
            title=f"{meta.get('title')} (text)",
            parent_artifact_id=artifact_id,
            parse_status="parsed",
            org_id=org_id,
            mime_type="text/plain",
        )
    await set_parse_status(artifact_id, status)
    return doc.full_text, status == "failed"


async def _set_index_status(source_id: str, org_id: str, status: str) -> None:
    project_sources = await reflect_table("project_sources")
    async with engine.begin() as conn:
        await conn.execute(
            update(project_sources)
            .where(
                project_sources.c.id == source_id,
                project_sources.c.organization_id == org_id,
            )
            .values(index_status=status, updated_at=func.now())
        )


async def delete_source_chunks(source_id: str, org_id: str) -> int:
    """Delete all chunks for a source (org-scoped). Returns the number deleted."""
    chunks = await reflect_table("project_source_chunks")
    async with engine.begin() as conn:
        result = await conn.execute(
            delete(chunks).where(
                chunks.c.source_id == source_id,
                chunks.c.organization_id == org_id,
            )
        )
    return result.rowcount or 0


async def index_source(source_id: str, org_id: str) -> dict:
    """Chunk + embed a project source's parsed text into project_source_chunks.

    Idempotent: deletes existing chunks for the source before inserting new ones.
    Sets project_sources.index_status to "indexed" (or "failed" on error). Returns
    a summary dict: {"source_id", "chunk_count", "index_status"}.
    """
    project_sources = await reflect_table("project_sources")
    async with engine.begin() as conn:
        source = (
            await conn.execute(
                select(project_sources).where(
                    project_sources.c.id == source_id,
                    project_sources.c.organization_id == org_id,
                )
            )
        ).mappings().first()
    if source is None:
        return {"source_id": source_id, "chunk_count": 0, "index_status": "not_found"}
    source = dict(source)

    # Resolve text BEFORE locking. _resolve_source_text parses + does artifact I/O
    # and opens its own transactions, so it must not run inside the source lock.
    try:
        full_text, parse_failed = await _resolve_source_text(source, org_id)
    except Exception as exc:
        await _set_index_status(source_id, org_id, "failed")
        await audit.log(
            "source_index_failed",
            "chronos",
            "source_indexing.index",
            resource_type="project_sources",
            resource_id=source_id,
            payload={"error": str(exc)[:240]},
            decision="parse_error",
        )
        return {"source_id": source_id, "chunk_count": 0, "index_status": "failed"}

    # Compute ALL chunk embeddings first (no DB writes yet). On a dimension
    # mismatch we mark failed and write nothing — an honest, all-or-nothing index.
    chunk_texts = _chunk_text(full_text)
    rows: list[dict] = []
    if not parse_failed:
        try:
            for idx, content in enumerate(chunk_texts):
                vector = await embed(content)
                if len(vector) != EXPECTED_EMBEDDING_DIMENSIONS:
                    raise ValueError(
                        f"embedding dimension mismatch: got {len(vector)}, "
                        f"expected {EXPECTED_EMBEDDING_DIMENSIONS}"
                    )
                rows.append(
                    {
                        "organization_id": org_id,
                        "region": settings.region,
                        "project_id": source.get("project_id"),
                        "source_id": source_id,
                        "chunk_index": idx,
                        "content": content,
                        "embedding": vector_literal(vector),
                        "token_count": len(content) // 4,
                    }
                )
        except Exception as exc:
            await _set_index_status(source_id, org_id, "failed")
            await audit.log(
                "source_index_failed",
                "chronos",
                "source_indexing.index",
                resource_type="project_sources",
                resource_id=source_id,
                payload={"error": str(exc)[:240]},
                decision="embedding_error",
            )
            return {"source_id": source_id, "chunk_count": 0, "index_status": "failed"}

    chunks = await reflect_table("project_source_chunks")
    status = "failed" if parse_failed else "indexed"

    # Single critical section: lock the source row, clear prior chunks, insert the
    # new ones, and set status — all in one transaction so the FOR UPDATE lock is
    # held across the whole delete-then-insert and the commit is all-or-nothing.
    try:
        async with engine.begin() as conn:
            # Lock the source row to serialize concurrent index_source calls.
            await conn.execute(
                select(project_sources)
                .where(
                    project_sources.c.id == source_id,
                    project_sources.c.organization_id == org_id,
                )
                .with_for_update()
            )
            # Idempotency: clear prior chunks before (re)inserting.
            await conn.execute(
                delete(chunks).where(
                    chunks.c.source_id == source_id,
                    chunks.c.organization_id == org_id,
                )
            )
            if rows:
                await conn.execute(insert(chunks), rows)
            await conn.execute(
                update(project_sources)
                .where(
                    project_sources.c.id == source_id,
                    project_sources.c.organization_id == org_id,
                )
                .values(index_status=status, updated_at=func.now())
            )
    except Exception as exc:
        # The transaction auto-rolled back, so no partial chunks are committed.
        # Record the failure in a fresh small transaction.
        await _set_index_status(source_id, org_id, "failed")
        await audit.log(
            "source_index_failed",
            "chronos",
            "source_indexing.index",
            resource_type="project_sources",
            resource_id=source_id,
            payload={"error": str(exc)[:240]},
            decision="embedding_error",
        )
        return {"source_id": source_id, "chunk_count": 0, "index_status": "failed"}

    if parse_failed:
        return {"source_id": source_id, "chunk_count": 0, "index_status": "failed"}

    inserted = len(rows)
    await audit.log(
        "source_indexed",
        "chronos",
        "source_indexing.index",
        resource_type="project_sources",
        resource_id=source_id,
        payload={"chunk_count": inserted},
    )
    return {"source_id": source_id, "chunk_count": inserted, "index_status": "indexed"}
