import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, insert, or_, select, update

from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.context import assemble_context
from core.db import engine, reflect_table
from core.intent import classify_intent
from core.llm import available_chat_models, normalize_chat_model, stream_completion
from core.memory_writes import create_memory_entry, extract_explicit_memory_content
from core.modes import normalize_mode
from core.models import Member, RequesterContext
from core.redis import redis_client
from memory.extraction import extract_and_save
from runtime.agent_loop import format_task_answer
from runtime.executor import TaskExecutor, activity_channel
from routers.tasks import create_task_record

logger = logging.getLogger(__name__)

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
    mode: str | None = None
    persona_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None


async def _create_conversation(member: Member, title: str, project_id: str | None = None) -> str:
    conversations = await reflect_table("conversations")
    values: dict[str, Any] = dict(
        organization_id=settings.org_id,
        region=settings.region,
        member_id=member.id,
        title=title[:80],
    )
    if project_id is not None:
        values["project_id"] = project_id
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(conversations)
            .values(**values)
            .returning(conversations.c.id)
        )
        return str(result.scalar_one())


async def _save_message(
    conversation_id: str,
    role: str,
    content: str,
    *,
    model: str | None = None,
    mode: str | None = None,
    citations: list | None = None,
    tool_traces: list | None = None,
    memory_refs: list | None = None,
    artifact_refs: list | None = None,
    approval_state: str | None = None,
    runtime_status: str | None = None,
    parent_message_id: str | None = None,
    pinned: bool | None = None,
) -> None:
    messages = await reflect_table("messages")
    conversations = await reflect_table("conversations")
    values: dict[str, Any] = dict(
        organization_id=settings.org_id,
        region=settings.region,
        conversation_id=conversation_id,
        role=role,
        content=content,
        token_count=len(content.split()),
        model=model,
        mode=mode,
        citations=citations if citations is not None else [],
        tool_traces=tool_traces if tool_traces is not None else [],
        memory_refs=memory_refs if memory_refs is not None else [],
        artifact_refs=artifact_refs if artifact_refs is not None else [],
        approval_state=approval_state,
        runtime_status=runtime_status,
        parent_message_id=parent_message_id,
    )
    if pinned is not None:
        values["pinned"] = pinned
    async with engine.begin() as conn:
        await conn.execute(insert(messages).values(**values))
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


# ─── Per-message action helpers ──────────────────────────────────────────────

async def _verify_conversation_ownership(conn, conversations, conversation_id: str, member) -> dict:
    """Fetch the conversation row and verify the calling member owns it.

    Raises HTTPException 404 if not found or owned by a different member/org.
    """
    row = (
        await conn.execute(
            select(conversations).where(
                conversations.c.id == conversation_id,
                conversations.c.member_id == member.id,
                conversations.c.organization_id == member.organization_id,
            )
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return dict(row)


async def _fetch_message(conn, messages, message_id: str, conversation_id: str) -> dict:
    """Fetch a message row by id and conversation_id. Raises 404 if missing."""
    row = (
        await conn.execute(
            select(messages).where(
                messages.c.id == message_id,
                messages.c.conversation_id == conversation_id,
            )
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return dict(row)


# ─── Pin / Unpin ──────────────────────────────────────────────────────────────

@router.post("/conversations/{conversation_id}/messages/{message_id}/pin")
async def pin_message(
    conversation_id: str,
    message_id: str,
    member=Depends(get_current_member),
) -> dict:
    await permissions.check(member, "pin_message", conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        await _verify_conversation_ownership(conn, conversations, conversation_id, member)
        await _fetch_message(conn, messages, message_id, conversation_id)
        await conn.execute(
            update(messages)
            .where(messages.c.id == message_id)
            .values(pinned=True)
        )
    await audit.log(
        "message_pinned",
        member.id,
        "chat.pin_message",
        resource_type="messages",
        resource_id=message_id,
    )
    return {"message_id": message_id, "pinned": True}


@router.post("/conversations/{conversation_id}/messages/{message_id}/unpin")
async def unpin_message(
    conversation_id: str,
    message_id: str,
    member=Depends(get_current_member),
) -> dict:
    await permissions.check(member, "unpin_message", conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        await _verify_conversation_ownership(conn, conversations, conversation_id, member)
        await _fetch_message(conn, messages, message_id, conversation_id)
        await conn.execute(
            update(messages)
            .where(messages.c.id == message_id)
            .values(pinned=False)
        )
    await audit.log(
        "message_unpinned",
        member.id,
        "chat.unpin_message",
        resource_type="messages",
        resource_id=message_id,
    )
    return {"message_id": message_id, "pinned": False}


# ─── Edit message ─────────────────────────────────────────────────────────────

class EditMessageRequest(BaseModel):
    content: str


@router.patch("/conversations/{conversation_id}/messages/{message_id}")
async def edit_message(
    conversation_id: str,
    message_id: str,
    req: EditMessageRequest,
    member=Depends(get_current_member),
) -> dict:
    await permissions.check(member, "edit_message", conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        await _verify_conversation_ownership(conn, conversations, conversation_id, member)
        msg = await _fetch_message(conn, messages, message_id, conversation_id)
        if msg.get("role") != "user":
            raise HTTPException(status_code=400, detail="Only user messages can be edited")
        await conn.execute(
            update(messages)
            .where(messages.c.id == message_id)
            .values(content=req.content)
        )
    await audit.log(
        "message_edited",
        member.id,
        "chat.edit_message",
        resource_type="messages",
        resource_id=message_id,
        payload={"prev_length": len(msg["content"]), "new_length": len(req.content)},
    )
    return {"message_id": message_id, "content": req.content}


# ─── Branch conversation ───────────────────────────────────────────────────────

@router.post("/conversations/{conversation_id}/messages/{message_id}/branch")
async def branch_conversation(
    conversation_id: str,
    message_id: str,
    member=Depends(get_current_member),
) -> dict:
    await permissions.check(member, "branch_conversation", conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")

    _COPY_COLS = [
        "role", "content", "token_count", "model", "mode", "citations",
        "tool_traces", "memory_refs", "artifact_refs", "approval_state",
        "runtime_status", "pinned",
    ]

    async with engine.begin() as conn:
        conv = await _verify_conversation_ownership(conn, conversations, conversation_id, member)
        src_msg = await _fetch_message(conn, messages, message_id, conversation_id)

        # Fetch all messages up to and including the branch point.
        # Use (created_at < src) OR (id == src) so ties on the same timestamp
        # are broken deterministically and the branch-point message is always
        # included exactly once.
        prior_rows = (
            await conn.execute(
                select(messages)
                .where(
                    messages.c.conversation_id == conversation_id,
                    or_(
                        messages.c.created_at < src_msg["created_at"],
                        messages.c.id == src_msg["id"],
                    ),
                )
                .order_by(messages.c.created_at.asc(), messages.c.id.asc())
            )
        ).mappings().all()

        # Create the new conversation
        new_conv_result = await conn.execute(
            insert(conversations)
            .values(
                organization_id=settings.org_id,
                region=settings.region,
                member_id=member.id,
                title=(conv.get("title") or "Branch")[:80],
            )
            .returning(conversations.c.id)
        )
        new_conv_id = str(new_conv_result.scalar_one())

        # Copy messages, setting parent_message_id to original row's id
        for row in prior_rows:
            row_dict = dict(row)
            copy_values: dict[str, Any] = dict(
                organization_id=settings.org_id,
                region=settings.region,
                conversation_id=new_conv_id,
                parent_message_id=row_dict["id"],
            )
            for col in _COPY_COLS:
                if col in row_dict:
                    copy_values[col] = row_dict[col]
            await conn.execute(insert(messages).values(**copy_values))

    await audit.log(
        "conversation_branched",
        member.id,
        "chat.branch_conversation",
        resource_type="conversations",
        resource_id=conversation_id,
        payload={"new_conversation_id": new_conv_id, "branch_point_message_id": message_id},
    )
    return {"conversation_id": new_conv_id}


# ─── Save message to memory ───────────────────────────────────────────────────

class SaveMemoryRequest(BaseModel):
    scope: str = "org"


@router.post("/conversations/{conversation_id}/messages/{message_id}/save-memory")
async def save_message_to_memory(
    conversation_id: str,
    message_id: str,
    req: SaveMemoryRequest,
    member=Depends(get_current_member),
) -> dict:
    await permissions.check(member, "save_message_to_memory", conversation_id)
    if req.scope not in {"org", "personal"}:
        raise HTTPException(status_code=400, detail="scope must be 'org' or 'personal'")
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        await _verify_conversation_ownership(conn, conversations, conversation_id, member)
        msg = await _fetch_message(conn, messages, message_id, conversation_id)

    scope_id = member.id if req.scope == "personal" else member.organization_id
    requester_context = RequesterContext.from_member(member)
    entry_id = await create_memory_entry(
        content=msg["content"],
        requester_context=requester_context,
        source="explicit",
        scope=req.scope,
        scope_id=scope_id,
        importance_score=0.8,
        conversation_id=conversation_id,
        created_by=member.id,
    )
    await audit.log(
        "message_saved_to_memory",
        member.id,
        "chat.save_message_to_memory",
        resource_type="messages",
        resource_id=message_id,
        payload={"memory_entry_id": entry_id, "scope": req.scope},
    )
    return {"memory_entry_id": entry_id}


# ─── Convert message to task ──────────────────────────────────────────────────

class ConvertTaskRequest(BaseModel):
    model: str | None = None
    mode: str | None = None


@router.post("/conversations/{conversation_id}/messages/{message_id}/convert-task")
async def convert_message_to_task(
    conversation_id: str,
    message_id: str,
    req: ConvertTaskRequest,
    member=Depends(get_current_member),
) -> dict:
    await permissions.check(member, "convert_message_to_task", conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        await _verify_conversation_ownership(conn, conversations, conversation_id, member)
        msg = await _fetch_message(conn, messages, message_id, conversation_id)

    task_id = await create_task_record(
        goal=msg["content"],
        member=member,
        triggered_by=conversation_id,
        model=req.model,
        mode=req.mode,
    )
    await audit.log(
        "message_converted_to_task",
        member.id,
        "chat.convert_message_to_task",
        resource_type="messages",
        resource_id=message_id,
        payload={"task_id": task_id},
    )
    return {"task_id": task_id}


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


def _normalize_traces(raw_traces: list[dict]) -> list[dict]:
    """Convert raw pubsub event dicts into the frontend ToolTrace shape.

    Mirrors the live SSE logic in chat/page.tsx so persisted traces render
    identically on reload: one entry per tool call, with the final status.

    ToolTrace shape: {id: str, tool: str, summary: str, status: str}
    """
    result: list[dict] = []
    for i, event in enumerate(raw_traces):
        event_type = event.get("type", "")
        tool = event.get("tool", "") or (
            "think" if event_type == "step_start" else
            "approval" if event_type == "awaiting_approval" else
            ""
        )

        # Determine summary and status from event type
        if event_type == "tool_call":
            summary = f"{tool.replace('.', ' ').replace('_', ' ')}…"
            status = "streaming"
        elif event_type == "tool_result":
            summary = event.get("summary") or f"{tool} done"
            status = "complete"
        elif event_type == "tool_error":
            summary = event.get("error") or f"{tool} failed"
            status = "error"
        elif event_type == "step_start":
            step = event.get("step") or {}
            summary = (step.get("description") if isinstance(step, dict) else None) or "Thinking…"
            status = "streaming"
        elif event_type == "step_done":
            summary = event.get("summary") or "Step complete"
            status = "complete"
        elif event_type == "awaiting_approval":
            ids = event.get("approval_ids") or []
            summary = f"Waiting for approval on {len(ids)} item(s)"
            status = "approval_pending"
        elif event_type == "sub_agent_spawned":
            summary = f"Sub-agent: {event.get('goal') or 'working'}"
            status = "streaming"
        elif event_type == "sub_agent_complete":
            summary = "Sub-agent finished"
            status = "complete"
        else:
            # thinking or unknown — skip; these are transient heartbeats
            continue

        # Mirror the frontend merge: tool_result/tool_error/step_done update the
        # most-recent streaming entry for the same tool, rather than appending.
        if event_type in {"tool_result", "tool_error"}:
            for j in range(len(result) - 1, -1, -1):
                if result[j]["tool"] == tool and result[j]["status"] == "streaming":
                    result[j] = {**result[j], "summary": summary, "status": status}
                    break
            else:
                result.append({"id": f"t{i}", "tool": tool, "summary": summary, "status": status})
        elif event_type == "step_done":
            for j in range(len(result) - 1, -1, -1):
                if result[j]["tool"] == "think" and result[j]["status"] == "streaming":
                    result[j] = {**result[j], "summary": summary, "status": "complete"}
                    break
            else:
                result.append({"id": f"t{i}", "tool": tool, "summary": summary, "status": status})
        else:
            result.append({"id": f"t{i}", "tool": tool, "summary": summary, "status": status})

    return result


async def _agent_loop_stream(
    *,
    conversation_id: str,
    goal: str,
    member: Member,
    persona_id: str | None,
    workspace_id: str | None,
    model: str | None,
    mode: str | None = None,
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
    task_id = await create_task_record(
        goal=goal,
        member=member,
        triggered_by=conversation_id,
        persona_id=persona_id,
        workspace_id=workspace_id,
        model=model,
        mode=mode,
    )

    # Subscribe BEFORE firing executor to guarantee no events are missed.
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(activity_channel(task_id))
    asyncio.create_task(TaskExecutor().run(task_id))

    yield f"data: {json.dumps({'type': 'conversation', 'conversation_id': conversation_id})}\n\n"
    yield f"data: {json.dumps({'type': 'task_created', 'task_id': task_id})}\n\n"

    TRACE_TYPES = {
        "tool_call", "tool_result", "tool_error", "step_start", "step_done",
        "awaiting_approval", "sub_agent_spawned", "sub_agent_complete", "thinking",
    }
    final_answer: str | None = None
    task_succeeded: bool = True
    # message_id inserted by agent_loop._save_assistant_message — used for the UPDATE.
    persisted_message_id: str | None = None
    # Collect raw trace and artifact events for post-loop metadata persistence.
    collected_traces: list[dict] = []
    collected_artifact_refs: list[dict] = []
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=60.0)
            if message is None:
                await asyncio.sleep(0.05)
                continue
            event: dict[str, Any] = json.loads(message["data"])
            event_type = event.get("type", "")
            if event_type in TRACE_TYPES:
                collected_traces.append(event)
                yield f"data: {json.dumps({'type': 'trace', 'event': event})}\n\n"
            elif event_type == "artifact":
                art = event
                collected_artifact_refs.append({
                    "id": art.get("artifact_id", ""),
                    "title": art.get("title", ""),
                    "kind": art.get("kind", "file"),
                    "mime_type": art.get("mime_type"),
                    "size_bytes": art.get("size_bytes"),
                })
                yield f"data: {json.dumps({'type': 'artifact', 'artifact': event})}\n\n"
            elif event_type == "task_complete":
                final_answer = format_task_answer(event.get("result") or {})
                persisted_message_id = event.get("message_id") or None
                task_succeeded = True
                break
            elif event_type == "task_failed":
                final_answer = f"The task stopped: {event.get('error') or 'unknown error'}"
                persisted_message_id = event.get("message_id") or None
                task_succeeded = False
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

        # Best-effort: attach collected metadata to the just-persisted assistant row.
        # Only update when we have the exact message_id from the loop — avoids any
        # concurrency ambiguity. If the id is absent (e.g. sub-agent path), skip.
        if persisted_message_id:
            try:
                messages_tbl = await reflect_table("messages")
                runtime_status = "complete" if task_succeeded else "error"
                normalized_traces = _normalize_traces(collected_traces)
                async with engine.begin() as conn:
                    await conn.execute(
                        update(messages_tbl)
                        .where(messages_tbl.c.id == persisted_message_id)
                        .values(
                            tool_traces=normalized_traces,
                            artifact_refs=collected_artifact_refs,
                            model=model,
                            mode=mode,
                            runtime_status=runtime_status,
                        )
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to attach metadata to message %s in conversation %s: %s",
                    persisted_message_id,
                    conversation_id,
                    exc,
                )

        if requester_context is not None and user_message_for_memory is not None:
            asyncio.create_task(
                extract_and_save(conversation_id, user_message_for_memory, final_answer, requester_context)
            )

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


@router.post("/message")
async def send_message(req: ChatRequest, member: Member = Depends(get_current_member)) -> StreamingResponse:
    await permissions.check(member, "chat", req.conversation_id or "new_conversation")
    selected_model = normalize_chat_model(req.model)
    normalized_mode = normalize_mode(req.mode)
    conversation_id = req.conversation_id or await _create_conversation(
        member, req.message, project_id=req.project_id
    )
    await _save_message(conversation_id, "user", req.message)
    requester_context = RequesterContext.from_member(member)
    requester_context.persona_id = req.persona_id
    requester_context.workspace_id = req.workspace_id

    # Resolve project_id: prefer explicit request value; fall back to the
    # conversation row when re-opening an in-project conversation.
    if req.project_id is not None:
        requester_context.project_id = req.project_id
    elif req.conversation_id is not None:
        # Tight SELECT — only fetch project_id, org-scoped for defence-in-depth.
        conversations = await reflect_table("conversations")
        async with engine.begin() as conn:
            conv_row = (
                await conn.execute(
                    select(conversations.c.project_id).where(
                        conversations.c.id == req.conversation_id,
                        conversations.c.member_id == member.id,
                        conversations.c.organization_id == member.organization_id,
                    )
                )
            ).mappings().first()
        if conv_row is not None:
            requester_context.project_id = conv_row["project_id"]
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
            await _save_message(conversation_id, "assistant", assistant_response, mode=normalized_mode)
            await audit.log("chat_response", member.id, "chat.message", resource_id=conversation_id)
            yield f"data: {json.dumps({'type': 'conversation', 'conversation_id': conversation_id})}\n\n"
            yield f"data: {json.dumps({'type': 'memory_saved', 'entry_id': entry_id, 'content': explicit_memory, 'scope': 'org', 'source': 'explicit'})}\n\n"
            yield f"data: {json.dumps({'type': 'token', 'content': assistant_response})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(explicit_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    intent = await classify_intent(req.message)

    # Tool-capable path: explicit "task" goals AND ordinary non-trivial chat both
    # run the agent loop so chat can search and act inline (Option 1). Only clearly
    # trivial, tool-free messages fall through to the fast token-streamed completion.
    route_through_loop = intent["mode"] == "task" or not _is_trivial_chat(req.message)
    if route_through_loop:
        is_task = intent["mode"] == "task"
        return StreamingResponse(
            _agent_loop_stream(
                conversation_id=conversation_id,
                goal=(intent.get("goal") or req.message) if is_task else req.message,
                member=member,
                persona_id=req.persona_id,
                workspace_id=req.workspace_id,
                model=req.model,
                mode=normalized_mode,
                # Chat-routed runs keep autonomous memory extraction; explicit tasks do not.
                requester_context=None if is_task else requester_context,
                user_message_for_memory=None if is_task else req.message,
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
        await _save_message(
            conversation_id, "assistant", assistant_response,
            model=selected_model,
            mode=normalized_mode,
        )
        await audit.log("chat_response", member.id, "chat.message", resource_id=conversation_id)
        asyncio.create_task(
            extract_and_save(conversation_id, req.message, assistant_response, requester_context)
        )
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)
