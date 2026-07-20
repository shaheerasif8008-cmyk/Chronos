"""Task 11 — connector-synced knowledge.

Pulls documents from a connector into project_sources rows and indexes them via
the Task 9 pipeline (memory.source_indexing.index_source). Every connector fetch
goes through the tool broker seam — this module NEVER imports or calls a connector
directly. A failed sync is reported honestly (index_status="failed"); documents are
never fabricated. Revoking a connector deletes its chunks so retrieval loses access.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.sql import func

from core import audit, tool_broker
from core.config import settings
from core.content_disarm import disarm_connector_binary
from core.db import engine, reflect_table
from core.file_security import (
    FileScanResult,
    record_file_security_event_if_available,
    scan_file_bytes,
)
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

    Rows are keyed by (parent feed, uri==external_id, org). A re-sync of
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
                    project_sources.c.parent_source_id == feed.get("id"),
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
                parent_source_id=feed.get("id"),
                source_type="connector",
                connector_id=connector_id,
                title=title,
                uri=external_id,
                artifact_id=artifact_id,
                permissions=permissions,
                parse_status="parsed",
                index_status="pending",
                created_by=str(feed.get("created_by") or "chronos"),
            )
            .returning(project_sources.c.id)
        )
        return str(result.scalar_one())


async def _validate_feed_access(feed: dict, org_id: str) -> tuple[bool, str]:
    """Re-prove the feed owner, project membership, and connector binding."""
    from core.connector_tools import member_connector_clause

    creator_id = str(feed.get("created_by") or "")
    connector_id = str(feed.get("connector_id") or "")
    if not creator_id or not connector_id:
        return False, "missing_owner_or_connector"
    members = await reflect_table("members")
    project_members = await reflect_table("project_members")
    connectors = await reflect_table("connectors")
    async with engine.begin() as conn:
        active_member = (
            await conn.execute(
                select(members.c.id)
                .select_from(
                    members.join(
                        project_members,
                        and_(
                            project_members.c.member_id == members.c.id,
                            project_members.c.organization_id == members.c.organization_id,
                        ),
                    )
                )
                .where(
                    members.c.id == creator_id,
                    members.c.organization_id == org_id,
                    members.c.status == "active",
                    project_members.c.project_id == feed.get("project_id"),
                )
            )
        ).first()
        connector = (
            await conn.execute(
                select(connectors.c.provider).where(
                    connectors.c.id == connector_id,
                    connectors.c.organization_id == org_id,
                    connectors.c.status == "active",
                    member_connector_clause(connectors, org_id, creator_id),
                )
            )
        ).mappings().first()
    if active_member is None:
        return False, "owner_inactive_or_project_access_revoked"
    if connector is None:
        return False, "connector_revoked_or_not_owned"
    spec = feed.get("permissions") if isinstance(feed.get("permissions"), dict) else {}
    tool = str(spec.get("tool") or "")
    requested_provider = tool.split("__", 1)[0].split(".", 1)[0]
    if requested_provider != str(connector.get("provider") or ""):
        return False, "connector_tool_mismatch"
    return True, "authorized"


async def _delete_stale_feed_documents(
    feed: dict,
    org_id: str,
    external_ids: set[str],
) -> int:
    """Remove documents that disappeared upstream, only for this exact feed."""
    project_sources = await reflect_table("project_sources")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(project_sources.c.id, project_sources.c.uri).where(
                    project_sources.c.organization_id == org_id,
                    project_sources.c.parent_source_id == feed.get("id"),
                )
            )
        ).mappings().all()
    stale_ids = [
        str(row["id"])
        for row in rows
        if str(row.get("uri") or "") not in external_ids
    ]
    for stale_id in stale_ids:
        await delete_source_chunks(stale_id, org_id)
    if stale_ids:
        async with engine.begin() as conn:
            await conn.execute(
                delete(project_sources).where(
                    project_sources.c.organization_id == org_id,
                    project_sources.c.parent_source_id == feed.get("id"),
                    project_sources.c.id.in_(stale_ids),
                )
            )
    return len(stale_ids)


# Connector ToolResults arrive in many shapes. These are the list-bearing keys
# we recognize, in priority order, so a sync doesn't have to be hand-tuned per
# connector. gmail.search returns {"threads": [...]}, MS Graph {"value": [...]},
# generic HTTP often {"items"/"results"/"rows": [...]}.
_LIST_KEYS = ("documents", "threads", "messages", "items", "results", "rows", "value", "records", "entries")
# Per-item field aliases → canonical (external_id, title, content).
_ID_KEYS = ("external_id", "id", "uri", "url", "key")
_TITLE_KEYS = ("title", "subject", "name", "summary", "snippet")
_CONTENT_KEYS = ("content", "body", "text", "snippet", "description")
_BINARY_KEYS = ("content_b64", "data_b64", "file_b64", "bytes_b64")
_MIME_KEYS = ("mime_type", "content_type", "media_type")
_FILENAME_KEYS = ("filename", "file_name", "name", "title")


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
    binary_value = _first(item, _BINARY_KEYS)
    content = _first(item, _CONTENT_KEYS)
    title = _first(item, _TITLE_KEYS) or (content[:80] if content else "") or "Untitled document"
    binary_content: bytes | None = None
    binary_error: str | None = None
    if binary_value:
        if len(binary_value) > ((settings.clamav_max_bytes + 2) // 3) * 4 + 8:
            binary_error = "connector_binary_size_limit"
        else:
            try:
                binary_content = base64.b64decode(binary_value, validate=True)
            except (binascii.Error, ValueError):
                binary_error = "connector_binary_decode_error"
    if not content and not binary_value:
        # Nothing text-like to index on its own — keep the record verbatim so the
        # indexer still has something honest to chunk rather than fabricating.
        content = json.dumps({k: v for k, v in item.items() if not str(k).startswith("__")}, default=str)
    canonical = {
        "external_id": external_id,
        "title": title,
        "content": content,
        "permissions": item.get("permissions") or {},
    }
    if binary_value:
        canonical.update(
            content_bytes=binary_content,
            binary_error=binary_error,
            binary_evidence=binary_value if binary_error else None,
            mime_type=_first(item, _MIME_KEYS) or "application/octet-stream",
            filename=_first(item, _FILENAME_KEYS) or title,
        )
    return canonical


def normalize_documents(data: dict | list) -> list[dict]:
    """Normalize a connector ToolResult payload into canonical docs.

    Generalizes beyond the ``{"documents": [...]}`` shape so any connector whose
    result carries a list of records (gmail threads, Graph ``value``, generic
    ``items``/``results``/``rows``) can sync without bespoke code.
    """
    return [_canonical_doc(item, i) for i, item in enumerate(_extract_items(data))]


async def _quarantine_feed_document(feed: dict, org_id: str, external_id: str) -> None:
    """Remove any previously indexed version when a replacement is unsafe."""

    project_sources = await reflect_table("project_sources")
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                select(project_sources.c.id).where(
                    project_sources.c.organization_id == org_id,
                    project_sources.c.parent_source_id == feed.get("id"),
                    project_sources.c.uri == external_id,
                )
            )
        ).first()
    if existing is None:
        return
    doc_id = str(existing[0])
    await delete_source_chunks(doc_id, org_id)
    async with engine.begin() as conn:
        await conn.execute(
            update(project_sources)
            .where(
                project_sources.c.id == doc_id,
                project_sources.c.organization_id == org_id,
            )
            .values(artifact_id=None, index_status="quarantined", updated_at=func.now())
        )


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

    authorized, access_reason = await _validate_feed_access(feed, org_id)
    if not authorized:
        await delete_source_chunks(source_id, org_id)
        await _set_index_status(source_id, org_id, "revoked")
        await audit.log(
            "source_sync_denied",
            str(feed.get("created_by") or "chronos"),
            "source_sync.sync",
            organization_id=org_id,
            resource_type="project_sources",
            resource_id=source_id,
            payload={"project_id": str(feed.get("project_id"))},
            decision=access_reason,
        )
        return {"source_id": source_id, "synced": 0, "index_status": "revoked"}

    spec = feed.get("permissions") or {}
    tool = spec.get("tool")
    args = spec.get("args") or {}
    if not tool:
        await _set_index_status(source_id, org_id, "failed")
        await audit.log(
            "source_sync_failed",
            "chronos",
            "source_sync.sync",
            organization_id=org_id,
            resource_type="project_sources",
            resource_id=source_id,
            payload={"project_id": str(feed.get("project_id"))},
            decision="missing_tool_spec",
        )
        return {"source_id": source_id, "synced": 0, "index_status": "failed"}

    # Fetch through the broker seam — honest on failure, never fabricated.
    # Connector credentials may be member-scoped.  Execute as the member who
    # registered the feed instead of the synthetic system identity; the broker
    # still records the source_sync agent id and enforces connector policy.
    feed_member_id = str(feed.get("created_by") or "chronos")
    agent = AgentContext(id="source_sync", org_id=org_id, member_id=feed_member_id)
    try:
        result = await tool_broker.execute(agent, tool, dict(args))
    except Exception as exc:
        await _set_index_status(source_id, org_id, "failed")
        await audit.log(
            "source_sync_failed",
            "chronos",
            "source_sync.sync",
            organization_id=org_id,
            resource_type="project_sources",
            resource_id=source_id,
            payload={"project_id": str(feed.get("project_id")), "error": str(exc)[:240]},
            decision="broker_error",
        )
        return {"source_id": source_id, "synced": 0, "index_status": "failed"}

    documents = normalize_documents(result.data or {})
    external_ids = {
        str(doc.get("external_id") or doc.get("id") or "") for doc in documents
    }
    removed = await _delete_stale_feed_documents(feed, org_id, external_ids)

    # Index each document. save_artifact is imported lazily to mirror the on-demand
    # artifact I/O elsewhere in the source pipeline and keep import order simple.
    from core.artifacts import save_artifact

    indexed = 0
    quarantined = 0
    any_failed = False
    for doc in documents:
        external_id = str(doc.get("external_id") or doc.get("id") or "")
        title = str(doc.get("title") or "Untitled document")
        content = doc.get("content") or ""
        doc_permissions = doc.get("permissions") or {}
        binary_error = str(doc.get("binary_error") or "")
        binary_content = doc.get("content_bytes")
        mime_type = str(doc.get("mime_type") or "application/octet-stream")
        filename = str(doc.get("filename") or title)
        scan: FileScanResult | None = None
        disarm_status = "not_applicable"
        disarm_reason: str | None = None
        if binary_error:
            evidence = str(doc.get("binary_evidence") or "").encode("utf-8")
            scan = FileScanResult(
                verdict="error",
                sha256=hashlib.sha256(evidence).hexdigest(),
                size_bytes=len(evidence),
                error_code=binary_error,
                scanned_at=datetime.now(timezone.utc),
            )
            await record_file_security_event_if_available(
                scan,
                organization_id=org_id,
                source="connector_sync",
                source_ref=external_id,
                filename=filename,
                mime_type=mime_type,
                created_by=feed_member_id,
                content_disarm_status="not_run",
            )
            await _quarantine_feed_document(feed, org_id, external_id)
            quarantined += 1
            any_failed = True
            continue
        if isinstance(binary_content, bytes):
            scan = await scan_file_bytes(binary_content)
            if scan.verdict != "clean":
                await record_file_security_event_if_available(
                    scan,
                    organization_id=org_id,
                    source="connector_sync",
                    source_ref=external_id,
                    filename=filename,
                    mime_type=mime_type,
                    created_by=feed_member_id,
                    content_disarm_status="not_run",
                )
                await _quarantine_feed_document(feed, org_id, external_id)
                quarantined += 1
                any_failed = True
                continue
            disarmed = await disarm_connector_binary(
                binary_content,
                filename=filename,
                mime_type=mime_type,
            )
            disarm_status = disarmed.status
            disarm_reason = disarmed.reason
            if disarmed.status != "sanitized" or disarmed.content is None:
                await record_file_security_event_if_available(
                    scan,
                    organization_id=org_id,
                    source="connector_sync",
                    source_ref=external_id,
                    filename=filename,
                    mime_type=mime_type,
                    created_by=feed_member_id,
                    content_disarm_status=disarmed.status,
                    content_disarm_reason=disarmed.reason,
                )
                await _quarantine_feed_document(feed, org_id, external_id)
                quarantined += 1
                any_failed = True
                continue
            content = disarmed.content
        artifact_id = await save_artifact(
            content,
            kind="connector_doc",
            title=title,
            org_id=org_id,
            mime_type="text/plain",
            parse_status="parsed",
            created_by=feed_member_id,
            malware_scan_status=scan.verdict if scan else "not_required",
            malware_scan_engine=scan.engine if scan else None,
            malware_scan_engine_version=scan.engine_version if scan else None,
            malware_scan_signature=scan.signature if scan else None,
            malware_scanned_at=scan.scanned_at if scan else None,
        )
        if scan is not None:
            await record_file_security_event_if_available(
                scan,
                organization_id=org_id,
                source="connector_sync",
                source_ref=external_id,
                filename=filename,
                mime_type=mime_type,
                created_by=feed_member_id,
                artifact_id=artifact_id,
                content_disarm_status=disarm_status,
                content_disarm_reason=disarm_reason,
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
        organization_id=org_id,
        resource_type="project_sources",
        resource_id=source_id,
        payload={
            "project_id": str(feed.get("project_id")),
            "connector_id": str(feed.get("connector_id")),
            "document_count": len(documents),
            "indexed": indexed,
            "removed": removed,
            "quarantined": quarantined,
        },
    )
    return {
        "source_id": source_id,
        "synced": len(documents),
        "indexed": indexed,
        "removed": removed,
        "quarantined": quarantined,
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
        organization_id=org_id,
        resource_type="project_sources",
        resource_id=connector_id,
        payload={"connector_id": connector_id, "revoked_count": len(source_ids)},
    )
    return len(source_ids)
