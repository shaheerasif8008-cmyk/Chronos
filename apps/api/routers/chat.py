import asyncio
import json
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, select, update

from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.context import assemble_context
from core.db import engine, reflect_table
from core.llm import available_chat_models
from core.memory_writes import create_memory_entry, extract_explicit_memory_content
from core.models import Member, RequesterContext
from core.artifacts import get_artifact as _get_artifact
from core.artifacts import read_artifact_content as _read_artifact_content
from core.artifacts import save_artifact as _save_artifact
from core.artifacts import set_parse_status as _set_parse_status
from runtime.agent_loop import stream_chat_turn

router = APIRouter(prefix="/chat", tags=["chat"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    model: str | None = None
    persona_id: str | None = None
    workspace_id: str | None = None
    attachment_ids: list[str] = Field(default_factory=list)


async def _create_conversation(member: Member, title: str) -> str:
    conversations = await reflect_table("conversations")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(conversations)
            .values(
                organization_id=settings.org_id,
                region=settings.region,
                member_id=member.id,
                title=title[:80],
            )
            .returning(conversations.c.id)
        )
        return str(result.scalar_one())


async def _save_message(conversation_id: str, role: str, content: str) -> None:
    messages = await reflect_table("messages")
    conversations = await reflect_table("conversations")
    async with engine.begin() as conn:
        await conn.execute(
            insert(messages).values(
                organization_id=settings.org_id,
                region=settings.region,
                conversation_id=conversation_id,
                role=role,
                content=content,
                token_count=len(content.split()),
            )
        )
        await conn.execute(
            update(conversations)
            .where(conversations.c.id == conversation_id)
            .values(updated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        )


@router.get("/models")
async def list_chat_models(member: Member = Depends(get_current_member)) -> list[dict[str, str]]:
    await permissions.check(member, "list_chat_models", settings.org_id)
    return available_chat_models()


@router.get("/conversations")
async def list_conversations(member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "list_conversations", settings.org_id)
    conversations = await reflect_table("conversations")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(conversations)
                .where(conversations.c.member_id == member.id)
                .order_by(conversations.c.updated_at.desc())
            )
        ).mappings().all()
    return [dict(row) for row in rows]


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(conversation_id: str, member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "view_conversation", conversation_id)
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(messages)
                .where(messages.c.conversation_id == conversation_id)
                .order_by(messages.c.created_at.asc())
            )
        ).mappings().all()
    return [dict(row) for row in rows]


@router.get("/conversations/{conversation_id}/latest-task")
async def latest_conversation_task(conversation_id: str, member: Member = Depends(get_current_member)) -> dict | None:
    await permissions.check(member, "view_conversation", conversation_id)
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(tasks)
                .where(
                    tasks.c.triggered_by == conversation_id,
                    tasks.c.organization_id == member.organization_id,
                    tasks.c.parent_task_id.is_(None),
                )
                .order_by(tasks.c.created_at.desc())
                .limit(1)
            )
        ).mappings().first()
    return dict(row) if row else None


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "delete_conversation", conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(conversations.c.id).where(
                    conversations.c.id == conversation_id,
                    conversations.c.member_id == member.id,
                    conversations.c.organization_id == member.organization_id,
                )
            )
        ).first()
        if row is None:
            return {"id": conversation_id, "deleted": False}
        await conn.execute(delete(messages).where(messages.c.conversation_id == conversation_id))
        await conn.execute(delete(conversations).where(conversations.c.id == conversation_id))
    await audit.log(
        "conversation_deleted",
        member.id,
        "chat.delete_conversation",
        resource_type="conversations",
        resource_id=conversation_id,
    )
    return {"id": conversation_id, "deleted": True}



async def _parse_attachments(attachment_ids: list[str], conversation_id: str, org_id: str) -> list[dict]:
    """Parse each not-yet-parsed attachment, store its text, return seed-context entries.

    Already-parsed attachments reuse their stored parsed_text artifact.
    """
    from sqlalchemy import select
    from core.db import engine, reflect_table
    from parsing.engine import PREVIEW_CHAR_LIMIT, UNPARSEABLE_NOTE, parse_document

    out: list[dict] = []
    for att_id in attachment_ids:
        meta = await _get_artifact(att_id)
        if not meta or str(meta.get("organization_id")) != str(org_id):
            continue

        # Cache hit: reuse the stored parsed_text child rather than re-parsing.
        if str(meta.get("parse_status")) == "parsed":
            artifacts = await reflect_table("artifacts")
            async with engine.begin() as conn:
                child = (
                    await conn.execute(
                        select(artifacts).where(
                            artifacts.c.parent_artifact_id == att_id,
                            artifacts.c.kind == "parsed_text",
                        )
                    )
                ).mappings().first()
            if child:
                full = (await _read_artifact_content(str(child["id"])) or b"").decode("utf-8", errors="replace")
                out.append({
                    "attachment_id": att_id,
                    "parsed_artifact_id": str(child["id"]),
                    "filename": meta.get("title"),
                    "preview": full[:PREVIEW_CHAR_LIMIT],
                    "truncated": len(full) > PREVIEW_CHAR_LIMIT,
                    "note": None,
                })
                await audit.log(
                    "attachment_parse_cache_hit", "system", "attachments.parse",
                    resource_type="artifacts", resource_id=att_id,
                )
                continue

        raw = await _read_artifact_content(att_id) or b""
        doc = await parse_document(raw, str(meta.get("mime_type") or ""), str(meta.get("title") or "file"))
        if doc.parser_used != "none":
            status = "parsed"
        elif doc.note == UNPARSEABLE_NOTE:
            status = "unparseable"
        else:
            status = "failed"
        parsed_artifact_id = None
        if doc.full_text:
            parsed_artifact_id = await _save_artifact(
                doc.full_text, kind="parsed_text", title=f"{meta.get('title')} (text)",
                conversation_id=conversation_id, parent_artifact_id=att_id,
                parse_status="parsed", org_id=org_id, mime_type="text/plain",
            )
        await _set_parse_status(att_id, status)
        await audit.log(
            "attachment_parsed", "system", "attachments.parse",
            resource_type="artifacts", resource_id=att_id,
            payload={"parse_status": status, "parser_used": doc.parser_used},
        )
        out.append({
            "attachment_id": att_id,
            "parsed_artifact_id": parsed_artifact_id,
            "filename": meta.get("title"),
            "preview": doc.preview,
            "truncated": doc.truncated,
            "note": doc.note,
        })
    return out


def _format_attachments_for_chat(attachments: list[dict]) -> str:
    lines = ["# Attached files", "The user attached these files. Their parsed text follows."]
    for a in attachments:
        lines.append(f"\n## {a.get('filename') or 'file'}\n{a.get('preview') or ''}")
    return "\n".join(lines)


@router.post("/message")
async def send_message(req: ChatRequest, member: Member = Depends(get_current_member)) -> StreamingResponse:
    await permissions.check(member, "chat", req.conversation_id or "new_conversation")
    conversation_id = req.conversation_id or await _create_conversation(member, req.message)
    await _save_message(conversation_id, "user", req.message)
    requester_context = RequesterContext.from_member(member)
    requester_context.persona_id = req.persona_id
    requester_context.workspace_id = req.workspace_id
    explicit_memory = extract_explicit_memory_content(req.message)

    if explicit_memory and not req.attachment_ids:
        async def explicit_stream():
            entry_id = await create_memory_entry(
                content=explicit_memory,
                requester_context=requester_context,
                source="explicit",
                scope="org",
                scope_id=member.organization_id,
                importance_score=0.9,
                conversation_id=conversation_id,
                created_by=member.id,
            )
            assistant_response = f"Got it, I'll remember that: {explicit_memory}"
            await _save_message(conversation_id, "assistant", assistant_response)
            await audit.log("chat_response", member.id, "chat.message", resource_id=conversation_id)
            yield f"data: {json.dumps({'type': 'conversation', 'conversation_id': conversation_id})}\n\n"
            yield f"data: {json.dumps({'type': 'memory_saved', 'entry_id': entry_id, 'content': explicit_memory, 'scope': 'org', 'source': 'explicit'})}\n\n"
            yield f"data: {json.dumps({'type': 'token', 'content': assistant_response})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(explicit_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    attachments_context: list[dict] = []
    if req.attachment_ids:
        attachments_context = await _parse_attachments(req.attachment_ids, conversation_id, member.organization_id)

    context = await assemble_context(conversation_id, req.message, requester_context)
    # assemble_context appends the user message last; drop it (stream_chat_turn re-adds it),
    # then inject any attachment text as a context message before the user's turn.
    context_messages = context[:-1]
    if attachments_context:
        context_messages.append({"role": "user", "content": _format_attachments_for_chat(attachments_context)})

    async def stream():
        async for ev in stream_chat_turn(
            conversation_id=conversation_id,
            message=req.message,
            context_messages=context_messages,
            requester_context=requester_context,
            model=req.model,
        ):
            yield f"data: {json.dumps(ev)}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)
