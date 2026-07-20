"""Attachment upload — stores raw bytes as an artifact.

Documents are parsed eagerly in the background after upload so the UI can show
honest ready/unreadable states up front; the chat path reuses the cached parse.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Form
from sqlalchemy import insert, select

from core import audit, permissions
from core.artifacts import ArtifactStorageUnavailable, save_artifact
from core.auth import get_current_member
from core.conversation_access import require_conversation
from core.config import settings
from core.content_disarm import inspect_active_content
from core.db import engine, reflect_table
from core.file_security import (
    FileScanUnavailable,
    record_file_security_event_if_available,
    require_safe_verdict,
    scan_file_bytes,
)
from core.models import Member

router = APIRouter(prefix="/attachments", tags=["attachments"])

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

# Strong references so eager-parse tasks aren't garbage-collected mid-flight.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


async def _parse_eagerly(attachment_id: str, conversation_id: str | None, org_id: str) -> None:
    """Parse a freshly uploaded document and record its parse status.

    Mirrors the lazy parse in the chat path (which reuses this result via the
    parse_status="parsed" cache check). Images/audio/video are skipped — they go
    through the vision/voice paths at use time, not the document parser.
    """
    from core.artifacts import get_artifact, read_artifact_content, set_parse_status
    from parsing.engine import UNPARSEABLE_NOTE, parse_document

    try:
        meta = await get_artifact(attachment_id)
        if not meta:
            return
        mime = str(meta.get("mime_type") or "")
        if mime.startswith(("image/", "audio/", "video/")):
            return
        raw = await read_artifact_content(attachment_id) or b""
        doc = await parse_document(raw, mime, str(meta.get("title") or "file"))
        if doc.parser_used != "none" and doc.full_text.strip():
            status = "parsed"
        elif doc.note == UNPARSEABLE_NOTE or not doc.full_text.strip():
            status = "unparseable"
        else:
            status = "failed"
        if doc.full_text:
            await save_artifact(
                doc.full_text, kind="parsed_text", title=f"{meta.get('title')} (text)",
                conversation_id=conversation_id, parent_artifact_id=attachment_id,
                parse_status="parsed", org_id=org_id, mime_type="text/plain",
                created_by=meta.get("created_by"),
            )
        await set_parse_status(attachment_id, status)
        await audit.log(
            "attachment_parsed", "system", "attachments.parse",
            organization_id=org_id,
            resource_type="artifacts", resource_id=attachment_id,
            payload={"parse_status": status, "parser_used": doc.parser_used, "eager": True},
        )
    except Exception:
        logger.exception("Eager parse failed for attachment %s", attachment_id)
        try:
            await set_parse_status(attachment_id, "failed")
        except Exception:
            pass


def _spawn_eager_parse(attachment_id: str, conversation_id: str | None, org_id: str) -> None:
    task = asyncio.create_task(_parse_eagerly(attachment_id, conversation_id, org_id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _spawn_source_index(source_id: str, org_id: str) -> None:
    """Keep project-source indexing alive for the lifetime of the request worker.

    The source row is durable and remains reindexable after a crash; this strong
    reference prevents Python from collecting the in-flight task during normal
    operation (the same lifecycle rule as eager document parsing above).
    """

    from memory.source_indexing import index_source

    task = asyncio.create_task(index_source(source_id, org_id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _require_conversation_member(member: Member, conversation_id: str) -> None:
    """Require editor access before linking an upload to a conversation."""

    try:
        await require_conversation(member, conversation_id, minimum="editor")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc


async def _require_task_member(member: Member, task_id: str) -> None:
    """Require canonical creator/assignee/admin visibility for task uploads."""

    from core.task_access import visible_task

    if await visible_task(member, task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")


async def _require_research_run_member(member: Member, research_run_id: str) -> None:
    """Raise 404 when *research_run_id* does not exist in the member's org.

    Args:
        member: The authenticated requester.
        research_run_id: The target research run UUID.

    Raises:
        HTTPException(404): When the research run does not belong to this org.
    """
    research_runs = await reflect_table("research_runs")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(research_runs.c.id).where(
                    research_runs.c.id == research_run_id,
                    research_runs.c.organization_id == member.organization_id,
                )
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Research run not found")


@router.post("")
async def upload_attachment(
    file: UploadFile = File(...),
    conversation_id: str | None = Form(default=None),
    project_id: str | None = Form(default=None),
    task_id: str | None = Form(default=None),
    research_run_id: str | None = Form(default=None),
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "upload_attachment", conversation_id or "new_conversation")
    # Enforce org-scoped ownership of the linked entity before storing anything.
    if conversation_id:
        await _require_conversation_member(member, conversation_id)
    if project_id:
        # Organization-visible projects are read-only to non-members. Uploads
        # create project source state, so they require explicit membership.
        from routers.projects import _require_editor
        await _require_editor(member, project_id)
        await permissions.check(member, "add_project_source", project_id)
    if task_id:
        await _require_task_member(member, task_id)
    if research_run_id:
        await _require_research_run_member(member, research_run_id)

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 25 MB limit")

    filename = file.filename or "upload"
    scan = await scan_file_bytes(raw)
    try:
        require_safe_verdict(scan)
    except ValueError as exc:
        event_id = await record_file_security_event_if_available(
            scan,
            organization_id=member.organization_id,
            source="attachment",
            filename=filename,
            mime_type=file.content_type,
            created_by=member.id,
            content_disarm_status="not_run",
        )
        await audit.log(
            "attachment_rejected_malware",
            member.id,
            "attachments.upload",
            organization_id=member.organization_id,
            resource_type="file_security_events",
            resource_id=event_id,
            payload={"verdict": "infected", "signature": scan.signature},
        )
        raise HTTPException(
            status_code=422,
            detail="This file was blocked because it contains malware.",
        ) from exc
    except FileScanUnavailable as exc:
        event_id = await record_file_security_event_if_available(
            scan,
            organization_id=member.organization_id,
            source="attachment",
            filename=filename,
            mime_type=file.content_type,
            created_by=member.id,
            content_disarm_status="not_run",
        )
        await audit.log(
            "attachment_scan_unavailable",
            member.id,
            "attachments.upload",
            organization_id=member.organization_id,
            resource_type="file_security_events",
            resource_id=event_id,
            payload={"verdict": "error", "error_code": exc.error_code},
        )
        raise HTTPException(
            status_code=503,
            detail="File security scanning is temporarily unavailable. Please retry.",
        ) from exc

    disarm = inspect_active_content(raw, filename=filename, mime_type=file.content_type)
    if disarm.status != "safe":
        event_id = await record_file_security_event_if_available(
            scan,
            organization_id=member.organization_id,
            source="attachment",
            filename=filename,
            mime_type=file.content_type,
            created_by=member.id,
            content_disarm_status=disarm.status,
            content_disarm_reason=disarm.reason,
        )
        await audit.log(
            "attachment_rejected_active_content",
            member.id,
            "attachments.upload",
            organization_id=member.organization_id,
            resource_type="file_security_events",
            resource_id=event_id,
            payload={"content_disarm_status": disarm.status, "reason": disarm.reason},
        )
        raise HTTPException(
            status_code=422,
            detail="This file contains active or embedded content that Chronos cannot safely disarm.",
        )

    try:
        attachment_id = await save_artifact(
            raw,
            kind="attachment",
            title=filename,
            conversation_id=conversation_id,
            task_id=task_id,
            mime_type=file.content_type,
            org_id=member.organization_id,
            parse_status="pending",
            created_by=member.id,
            malware_scan_status=scan.verdict,
            malware_scan_engine=scan.engine,
            malware_scan_engine_version=scan.engine_version,
            malware_scan_signature=scan.signature,
            malware_scanned_at=scan.scanned_at,
        )
    except ArtifactStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    event_id = await record_file_security_event_if_available(
        scan,
        organization_id=member.organization_id,
        source="attachment",
        filename=filename,
        mime_type=file.content_type,
        created_by=member.id,
        artifact_id=attachment_id,
        content_disarm_status="safe",
    )
    await audit.log(
        "attachment_uploaded", member.id, "attachments.upload",
        organization_id=member.organization_id,
        resource_type="artifacts", resource_id=attachment_id,
        payload={"file_security_event_id": event_id, "malware_scan_status": scan.verdict},
    )
    _spawn_eager_parse(attachment_id, conversation_id, member.organization_id)

    source_id = None
    if project_id:
        project_sources = await reflect_table("project_sources")
        async with engine.begin() as conn:
            src_result = await conn.execute(
                insert(project_sources)
                .values(
                    organization_id=member.organization_id,
                    region=settings.region,
                    project_id=project_id,
                    source_type="upload",
                    title=filename,
                    artifact_id=attachment_id,
                    parse_status="pending",
                    index_status="pending",
                    created_by=member.id,
                )
                .returning(project_sources.c.id)
            )
            source_id = str(src_result.scalar_one())
        await audit.log(
            "source_added", member.id, "attachments.add_source",
            organization_id=member.organization_id,
            resource_type="project_sources", resource_id=source_id,
            payload={"project_id": project_id},
        )
        # Fire-and-forget background index so upload returns immediately (still
        # index_status="pending"). index_source owns its error handling and marks
        # the source "failed" on error rather than crashing.
        _spawn_source_index(source_id, member.organization_id)

    if task_id:
        await audit.log(
            "attachment_linked_task", member.id, "attachments.link_task",
            organization_id=member.organization_id,
            resource_type="artifacts", resource_id=attachment_id,
            payload={"task_id": task_id},
        )

    if research_run_id:
        # research_runs has no artifact FK column; record the link in the audit log.
        await audit.log(
            "attachment_linked_research_run", member.id, "attachments.link_research_run",
            organization_id=member.organization_id,
            resource_type="artifacts", resource_id=attachment_id,
            payload={"research_run_id": research_run_id},
        )

    return {
        "attachment_id": attachment_id,
        "filename": file.filename,
        "mime_type": file.content_type,
        "size_bytes": len(raw),
        "source_id": source_id,
        "task_id": task_id,
        "research_run_id": research_run_id,
    }
