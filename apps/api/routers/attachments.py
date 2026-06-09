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
from core.artifacts import save_artifact
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
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
        if doc.parser_used != "none":
            status = "parsed"
        elif doc.note == UNPARSEABLE_NOTE:
            status = "unparseable"
        else:
            status = "failed"
        if doc.full_text:
            await save_artifact(
                doc.full_text, kind="parsed_text", title=f"{meta.get('title')} (text)",
                conversation_id=conversation_id, parent_artifact_id=attachment_id,
                parse_status="parsed", org_id=org_id, mime_type="text/plain",
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


async def _require_conversation_member(member: Member, conversation_id: str) -> None:
    """Raise 404 when *conversation_id* does not exist in the member's org.

    Enforces tenant isolation for conversation-linked uploads without relying
    on the permissions stub.

    Args:
        member: The authenticated requester.
        conversation_id: The target conversation UUID.

    Raises:
        HTTPException(404): When the conversation does not belong to this org.
    """
    conversations = await reflect_table("conversations")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(conversations.c.id).where(
                    conversations.c.id == conversation_id,
                    conversations.c.organization_id == member.organization_id,
                )
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")


async def _require_task_member(member: Member, task_id: str) -> None:
    """Raise 404 when *task_id* does not exist in the member's org.

    Args:
        member: The authenticated requester.
        task_id: The target task UUID.

    Raises:
        HTTPException(404): When the task does not belong to this org.
    """
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(tasks.c.id).where(
                    tasks.c.id == task_id,
                    tasks.c.organization_id == member.organization_id,
                )
            )
        ).first()
    if row is None:
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
        # Enforce membership before reading/storing (raises 404 for non-members).
        from routers.projects import _require_member
        await _require_member(member, project_id)
    if task_id:
        await _require_task_member(member, task_id)
    if research_run_id:
        await _require_research_run_member(member, research_run_id)

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 25 MB limit")

    attachment_id = await save_artifact(
        raw,
        kind="attachment",
        title=file.filename or "upload",
        conversation_id=conversation_id,
        task_id=task_id,
        mime_type=file.content_type,
        org_id=member.organization_id,
        parse_status="pending",
    )
    await audit.log(
        "attachment_uploaded", member.id, "attachments.upload",
        organization_id=member.organization_id,
        resource_type="artifacts", resource_id=attachment_id,
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
                    title=file.filename or "upload",
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
        from memory.source_indexing import index_source
        asyncio.create_task(index_source(source_id, member.organization_id))

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
