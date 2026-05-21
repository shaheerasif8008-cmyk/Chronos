import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, insert, select, update

from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.context import assemble_context
from core.db import engine, reflect_table
from core.llm import stream_completion
from core.memory_writes import create_memory_entry, extract_explicit_memory_content
from core.models import Member, RequesterContext
from memory.extraction import extract_and_save
from runtime.executor import TaskExecutor

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


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


def _looks_like_task(message: str) -> bool:
    lowered = message.lower()
    task_verbs = ("find ", "research ", "draft ", "create ", "build ", "prepare ", "analyze ", "summarize ")
    multi_step_markers = (" and ", " then ", " for each", "companies", "leads", "outreach")
    return any(verb in lowered for verb in task_verbs) and any(marker in lowered for marker in multi_step_markers)


def _is_operator_workflow_proof(message: str) -> bool:
    normalized = " ".join(message.lower().split())
    return "operator workflow proof" in normalized


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


@router.post("/message")
async def send_message(req: ChatRequest, member: Member = Depends(get_current_member)) -> StreamingResponse:
    await permissions.check(member, "chat", req.conversation_id or "new_conversation")
    conversation_id = req.conversation_id or await _create_conversation(member, req.message)
    await _save_message(conversation_id, "user", req.message)
    requester_context = RequesterContext.from_member(member)
    explicit_memory = extract_explicit_memory_content(req.message)

    if explicit_memory:
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

        return StreamingResponse(explicit_stream(), media_type="text/event-stream")

    if (settings.demo_mode and _looks_like_task(req.message)) or _is_operator_workflow_proof(req.message):
        async def task_stream():
            from routers.tasks import create_task_record

            task_id = await create_task_record(
                goal=req.message,
                member=member,
                triggered_by=conversation_id,
            )
            asyncio.create_task(TaskExecutor().run(task_id))
            assistant_response = "I started this as an autonomous task. You can watch live progress in Activity and approve drafts in Approvals."
            await _save_message(conversation_id, "assistant", assistant_response)
            await audit.log("chat_response", member.id, "chat.message", resource_id=conversation_id)
            yield f"data: {json.dumps({'type': 'conversation', 'conversation_id': conversation_id})}\n\n"
            yield f"data: {json.dumps({'type': 'token', 'content': assistant_response})}\n\n"
            yield f"data: {json.dumps({'type': 'task_created', 'task_id': task_id})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(task_stream(), media_type="text/event-stream")

    context = await assemble_context(conversation_id, req.message, requester_context)

    async def stream():
        yield f"data: {json.dumps({'type': 'conversation', 'conversation_id': conversation_id})}\n\n"
        full = ""
        async for token in stream_completion(context):
            full += token
            yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
            await asyncio.sleep(0)
        assistant_response = full.strip()
        await _save_message(conversation_id, "assistant", assistant_response)
        await audit.log("chat_response", member.id, "chat.message", resource_id=conversation_id)
        asyncio.create_task(
            extract_and_save(conversation_id, req.message, assistant_response, requester_context)
        )
        if _looks_like_task(req.message):
            from routers.tasks import create_task_record

            task_id = await create_task_record(
                goal=req.message,
                member=member,
                triggered_by=conversation_id,
            )
            asyncio.create_task(TaskExecutor().run(task_id))
            yield f"data: {json.dumps({'type': 'task_created', 'task_id': task_id})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
