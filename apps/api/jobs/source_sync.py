"""Task 11 — connector-synced knowledge.

Pulls documents from a connector into project_sources rows and indexes them via
the Task 9 pipeline (memory.source_indexing.index_source). Every connector fetch
goes through the tool broker seam — this module NEVER imports or calls a connector
directly. A failed sync is reported honestly (index_status="failed"); documents are
never fabricated. Revoking a connector deletes its chunks so retrieval loses access.
"""
from __future__ import annotations

import json

from sqlalchemy import delete, insert, select, update
from sqlalchemy.sql import func

from core import audit, tool_broker
from core.config import settings
from core.db import engine, reflect_table
from core.models import AgentContext
from memory.source_indexing import delete_source_chunks, index_source


async def _load_connector_source(source_id: str, org_id: str) -> dict | None:
    """Return the connector feed row (org-scoped) or None if missing/not a connector."""
    project_sources = await reflect_table("project_sources")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(project_sources).where(
                    project_sources.c.id == source_id,
                    project_sources.c.organization_id == org_id,
                )
            )
        ).mappings().first()
    if row is None:
        return None
    row = dict(row)
    if row.get("source_type") != "connector":
        return None
    return row


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


async def _upsert_doc_source(
    *,
    feed: dict,
    org_id: str,
    external_id: str,
    title: str,
    artifact_id: str,
    permissions: dict,
) -> str:
    """Create or update the per-document project_sources row; return its id.

    Rows are keyed by (connector_id, project_id, uri==external_id, org). A re-sync of
    the same external document updates the existing row rather than duplicating it.
    """
    project_sources = await reflect_table("project_sources")
    connector_id = feed.get("connector_id")
    project_id = feed.get("project_id")
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                select(project_sources.c.id).where(
                    project_sources.c.organization_id == org_id,
                    project_sources.c.project_id == project_id,
                    project_sources.c.connector_id == connector_id,
                    project_sources.c.uri == external_id,
                )
            )
        ).mappings().first()
        if existing is not None:
            doc_id = str(existing["id"])
            await conn.execute(
                update(project_sources)
                .where(
                    project_sources.c.id == doc_id,
                    project_sources.c.organization_id == org_id,
                )
                .values(
                    title=title,
                    artifact_id=artifact_id,
                    permissions=permissions,
                    parse_status="parsed",
                    index_status="pending",
                    updated_at=func.now(),
                )
            )
            return doc_id
        result = await conn.execute(
            insert(project_sources)
            .values(
                organization_id=org_id,
                region=settings.region,
                project_id=project_id,
                source_type="connector",
                connector_id=connector_id,
                title=title,
                uri=external_id,
                artifact_id=artifact_id,
                permissions=permissions,
                parse_status="parsed",
                index_status="pending",
                created_by="chronos",
            )
            .returning(project_sources.c.id)
        )
        return str(result.scalar_one())


# Connector ToolResults arrive in many shapes. These are the list-bearing keys
# we recognize, in priority order, so a sync doesn't have to be hand-tuned per
# connector. gmail.search returns {"threads": [...]}, MS Graph {"value": [...]},
# generic HTTP often {"items"/"results"/"rows": [...]}.
_LIST_KEYS = ("documents", "threads", "messages", "items", "results", "rows", "value", "records", "entries")
# Per-item field aliases → canonical (external_id, title, content).
_ID_KEYS = ("external_id", "id", "uri", "url", "key")
_TITLE_KEYS = ("title", "subject", "name", "summary", "snippet")
_CONTENT_KEYS = ("content", "body", "text", "snippet", "description")


def _extract_items(data: dict | list) -> list:
    """Find the list of records in a connector ToolResult payload."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in _LIST_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return value
    # Fall back to the first list-valued field, if any.
    for value in data.values():
        if isinstance(value, list):
            return value
    return []


def _first(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _canonical_doc(item: object, index: int) -> dict:
    """Map one connector record into the canonical {external_id, title, content} doc."""
    if not isinstance(item, dict):
        text = str(item)
        return {"external_id": f"doc-{index}", "title": text[:80] or "Untitled document", "content": text, "permissions": {}}
    external_id = _first(item, _ID_KEYS) or f"doc-{index}"
    content = _first(item, _CONTENT_KEYS)
    title = _first(item, _TITLE_KEYS) or (content[:80] if content else "") or "Untitled document"
    if not content:
        # Nothing text-like to index on its own — keep the record verbatim so the
        # indexer still has something honest to chunk rather than fabricating.
        content = json.dumps({k: v for k, v in item.items() if not str(k).startswith("__")}, default=str)
    return {
        "external_id": external_id,
        "title": title,
        "content": content,
        "permissions": item.get("permissions") or {},
    }


def normalize_documents(data: dict | list) -> list[dict]:
    """Normalize a connector ToolResult payload into canonical docs.

    Generalizes beyond the ``{"documents": [...]}`` shape so any connector whose
    result carries a list of records (gmail threads, Graph ``value``, generic
    ``items``/``results``/``rows``) can sync without bespoke code.
    """
    return [_canonical_doc(item, i) for i, item in enumerate(_extract_items(data))]


async def sync_connector_source(source_id: str, org_id: str) -> dict:
    """Pull documents for a connector feed and index each via the Task 9 pipeline.

    The feed row stores its fetch spec in ``permissions``: ``{"tool": "...", "args": {}}``.
    Documents are fetched through ``tool_broker.execute`` (never a direct connector call).
    The ToolResult payload is normalized (``normalize_documents``) into canonical
    ``{"external_id", "title", "content"}`` docs, so connectors that return
    ``threads``/``messages``/``items``/``value`` sync without bespoke code. A broker
    failure marks the feed failed and fabricates nothing.
    """
    feed = await _load_connector_source(source_id, org_id)
    if feed is None:
        return {"source_id": source_id, "synced": 0, "index_status": "not_found"}

    spec = feed.get("permissions") or {}
    tool = spec.get("tool")
    args = spec.get("args") or {}
    if not tool:
        await _set_index_status(source_id, org_id, "failed")
        await audit.log(
            "source_sync_failed",
            "chronos",
            "source_sync.sync",
            resource_type="project_sources",
            resource_id=source_id,
            payload={"project_id": str(feed.get("project_id"))},
            decision="missing_tool_spec",
        )
        return {"source_id": source_id, "synced": 0, "index_status": "failed"}

    # Fetch through the broker seam — honest on failure, never fabricated.
    agent = AgentContext(id="source_sync", org_id=org_id, member_id="chronos")
    try:
        result = await tool_broker.execute(agent, tool, dict(args))
    except Exception as exc:
        await _set_index_status(source_id, org_id, "failed")
        await audit.log(
            "source_sync_failed",
            "chronos",
            "source_sync.sync",
            resource_type="project_sources",
            resource_id=source_id,
            payload={"project_id": str(feed.get("project_id")), "error": str(exc)[:240]},
            decision="broker_error",
        )
        return {"source_id": source_id, "synced": 0, "index_status": "failed"}

    documents = normalize_documents(result.data or {})

    # Index each document. save_artifact is imported lazily to mirror the on-demand
    # artifact I/O elsewhere in the source pipeline and keep import order simple.
    from core.artifacts import save_artifact

    indexed = 0
    any_failed = False
    for doc in documents:
        external_id = str(doc.get("external_id") or doc.get("id") or "")
        title = str(doc.get("title") or "Untitled document")
        content = doc.get("content") or ""
        doc_permissions = doc.get("permissions") or {}
        artifact_id = await save_artifact(
            content,
            kind="connector_doc",
            title=title,
            org_id=org_id,
            mime_type="text/plain",
            parse_status="parsed",
        )
        doc_id = await _upsert_doc_source(
            feed=feed,
            org_id=org_id,
            external_id=external_id,
            title=title,
            artifact_id=artifact_id,
            permissions=doc_permissions,
        )
        summary = await index_source(doc_id, org_id)
        if summary.get("index_status") == "indexed":
            indexed += 1
        else:
            any_failed = True

    status = "synced" if not any_failed else "failed"
    await _set_index_status(source_id, org_id, status)
    await audit.log(
        "source_synced",
        "chronos",
        "source_sync.sync",
        resource_type="project_sources",
        resource_id=source_id,
        payload={
            "project_id": str(feed.get("project_id")),
            "connector_id": str(feed.get("connector_id")),
            "document_count": len(documents),
            "indexed": indexed,
        },
    )
    return {
        "source_id": source_id,
        "synced": len(documents),
        "indexed": indexed,
        "index_status": status,
    }


async def revoke_connector_sources(connector_id: str, org_id: str) -> int:
    """Revoke every source for a connector: delete its chunks and mark it revoked.

    Removing the chunks is what removes retrieval access; the rows are kept (marked
    "revoked") so the UI can show the degraded state honestly. Returns the count.
    """
    project_sources = await reflect_table("project_sources")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(project_sources.c.id).where(
                    project_sources.c.connector_id == connector_id,
                    project_sources.c.organization_id == org_id,
                )
            )
        ).mappings().all()
    source_ids = [str(row["id"]) for row in rows]

    for sid in source_ids:
        await delete_source_chunks(sid, org_id)

    if source_ids:
        async with engine.begin() as conn:
            await conn.execute(
                update(project_sources)
                .where(
                    project_sources.c.connector_id == connector_id,
                    project_sources.c.organization_id == org_id,
                )
                .values(index_status="revoked", updated_at=func.now())
            )

    await audit.log(
        "connector_sources_revoked",
        "chronos",
        "source_sync.revoke",
        resource_type="project_sources",
        resource_id=connector_id,
        payload={"connector_id": connector_id, "revoked_count": len(source_ids)},
    )
    return len(source_ids)
