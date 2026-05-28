import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, insert, select, update

from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.context import assemble_context
from core.db import engine, reflect_table
from core.intent import classify_intent
from core.llm import available_chat_models, normalize_chat_model, stream_completion
from core.memory_writes import create_memory_entry, extract_explicit_memory_content
from core.models import Member, RequesterContext
from core.redis import redis_client
from memory.extraction import extract_and_save
from runtime.agent_loop import format_task_answer
from runtime import task_runner
from runtime.executor import activity_channel

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


_TRIVIAL_CHAT_PHRASES = {
    "hi", "hello", "hey", "yo", "thanks", "thank you", "ty", "ok", "okay",
    "cool", "great", "nice", "got it", "yes", "no", "yep", "nope", "sure", "k",
}
_TOOL_HINT_WORDS = (
    "search", "find", "look", "latest", "news", "current", "draft", "send",
    "email", "write", "build", "check", "research", "summarize", "compare",
    "fetch", "browse", "today",
)


def _is_trivial_chat(message: str) -> bool:
    """Fast-path gate: only obviously conversational, tool-free messages skip the loop.

    Biased toward False — a misrouted tool-needing message would silently lose tool
    access, so any question or tool hint goes through the agent loop instead.
    """
    normalized = " ".join(message.lower().split()).strip(" .!")
    if not normalized:
        return True
    if normalized in _TRIVIAL_CHAT_PHRASES:
        return True
    if "?" in message or any(hint in normalized for hint in _TOOL_HINT_WORDS):
        return False
    return len(normalized.split()) <= 3


async def _agent_loop_stream(
    *,
    conversation_id: str,
    goal: str,
    member: Member,
    persona_id: str | None,
    workspace_id: str | None,
    model: str | None,
    requester_context: RequesterContext | None = None,
    user_message_for_memory: str | None = None,
):
    """Run a goal through the tool-capable agent loop and stream it as a chat reply.

    Shared by explicit "task" intent and ordinary (non-trivial) chat so both get
    inline tool use. Tool steps are surfaced as `trace` events; the final answer is
    streamed as tokens. The loop persists the answer to the conversation itself.
    When `requester_context`/`user_message_for_memory` are supplied (chat-routed
    runs), autonomous memory extraction fires too, matching the fast path.
    """
    from routers.tasks import create_task_record

    task_id = await create_task_record(
        goal=goal,
        member=member,
        triggered_by=conversation_id,
        persona_id=persona_id,
        workspace_id=workspace_id,
        model=model,
    )

    # Subscribe BEFORE firing executor, then wait briefly for subscription to
    # propagate so Redis doesn't miss the first events due to race condition.
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(activity_channel(task_id))
    await asyncio.sleep(0.1)
    await task_runner.enqueue_task(task_id)

    yield f"data: {json.dumps({'type': 'conversation', 'conversation_id': conversation_id})}\n\n"
    yield f"data: {json.dumps({'type': 'task_created', 'task_id': task_id})}\n\n"

    TRACE_TYPES = {
        "tool_call", "tool_result", "tool_error", "step_start", "step_done",
        "awaiting_approval", "sub_agent_spawned", "sub_agent_complete", "thinking",
        "route_decision", "model_step", "model_result", "reasoning_summary",
    }
    final_answer: str | None = None
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=60.0)
            if message is None:
                await asyncio.sleep(0.05)
                continue
            event: dict[str, Any] = json.loads(message["data"])
            event_type = event.get("type", "")
            if event_type in TRACE_TYPES:
                yield f"data: {json.dumps({'type': 'trace', 'event': event})}\n\n"
            elif event_type == "artifact":
                yield f"data: {json.dumps({'type': 'artifact', 'artifact': event})}\n\n"
            elif event_type == "task_complete":
                final_answer = format_task_answer(event.get("result") or {})
                break
            elif event_type == "task_failed":
                final_answer = f"The task stopped: {event.get('error') or 'unknown error'}"
                break
    finally:
        await pubsub.unsubscribe(activity_channel(task_id))
        await pubsub.close()

    # The agent loop already persisted the answer to the conversation (source of
    # truth); stream it for live display only.
    if final_answer:
        chunk_size = 40
        for i in range(0, len(final_answer), chunk_size):
            yield f"data: {json.dumps({'type': 'token', 'content': final_answer[i:i + chunk_size]})}\n\n"
            await asyncio.sleep(0)
        if requester_context is not None and user_message_for_memory is not None:
            asyncio.create_task(
                extract_and_save(conversation_id, user_message_for_memory, final_answer, requester_context)
            )

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


@router.post("/message")
async def send_message(req: ChatRequest, member: Member = Depends(get_current_member)) -> StreamingResponse:
    await permissions.check(member, "chat", req.conversation_id or "new_conversation")
    selected_model = normalize_chat_model(req.model)
    conversation_id = req.conversation_id or await _create_conversation(member, req.message)
    await _save_message(conversation_id, "user", req.message)
    requester_context = RequesterContext.from_member(member)
    requester_context.persona_id = req.persona_id
    requester_context.workspace_id = req.workspace_id
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

        return StreamingResponse(explicit_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    # Tool-capable path: explicit "task" goals AND ordinary non-trivial chat both
    # run the agent loop so chat can search and act inline (Option 1). Only clearly
    # trivial, tool-free messages fall through to the fast token-streamed completion.
    if not _is_trivial_chat(req.message):
        return StreamingResponse(
            _agent_loop_stream(
                conversation_id=conversation_id,
                goal=req.message,
                member=member,
                persona_id=req.persona_id,
                workspace_id=req.workspace_id,
                model=req.model,
                requester_context=requester_context,
                user_message_for_memory=req.message,
            ),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    intent = await classify_intent(req.message)
    if intent["mode"] == "task":
        return StreamingResponse(
            _agent_loop_stream(
                conversation_id=conversation_id,
                goal=intent.get("goal") or req.message,
                member=member,
                persona_id=req.persona_id,
                workspace_id=req.workspace_id,
                model=req.model,
            ),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    context = await assemble_context(conversation_id, req.message, requester_context)

    async def stream():
        yield f"data: {json.dumps({'type': 'conversation', 'conversation_id': conversation_id})}\n\n"
        full = ""
        async for token in stream_completion(context, model_id=selected_model):
            full += token
            yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
            await asyncio.sleep(0)
        assistant_response = full.strip()
        await _save_message(conversation_id, "assistant", assistant_response)
        await audit.log("chat_response", member.id, "chat.message", resource_id=conversation_id)
        asyncio.create_task(
            extract_and_save(conversation_id, req.message, assistant_response, requester_context)
        )
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)
