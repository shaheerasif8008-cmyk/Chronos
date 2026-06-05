from __future__ import annotations
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Coroutine

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, or_, select, update

from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.context import assemble_context
from core.db import engine, reflect_table
from core.intent import classify_intent
from core.context import (
    _IMAGE_MIMES,
    _VISION_UNAVAILABLE_NOTE,
    build_image_block,
    build_user_turn_content,
)
from core.llm import available_chat_models, model_supports_vision, normalize_chat_model
from core.modes import available_modes
from core.memory_writes import create_memory_entry, extract_explicit_memory_content
from core.models import Member, RequesterContext
from core.redis import redis_client
from core.artifacts import get_artifact as _get_artifact
from core.artifacts import read_artifact_content as _read_artifact_content
from core.artifacts import save_artifact as _save_artifact
from core.artifacts import set_parse_status as _set_parse_status
from memory.extraction import extract_and_save
from runtime.agent_loop import format_task_answer, stream_chat_turn
from runtime import task_runner
from runtime.executor import activity_channel
from routers.tasks import create_task_record
from routers.workflows import repository as workflow_repository
from routers.workflows import runtime as workflow_runtime

router = APIRouter(prefix="/chat", tags=["chat"])

logger = logging.getLogger(__name__)

# Hold strong references to background tasks so they aren't garbage-collected
# mid-flight (per the asyncio.create_task docs / RUF006).
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_background(coro: Coroutine, *, label: str) -> None:
    """Fire-and-forget a coroutine, logging any exception instead of swallowing it."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.exception("Background task %s failed", label, exc_info=t.exception())

    task.add_done_callback(_done)


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
    attachment_ids: list[str] = Field(default_factory=list)


async def _create_conversation(member: Member, title: str, project_id: str | None = None) -> str:
    conversations = await reflect_table("conversations")
    values: dict = dict(
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
    conversation_id: str, role: str, content: str, *,
    structured_response: dict | None = None,
    _member_id: str | None = None,
    _org_id: str | None = None,
    model: str | None = None,
    mode: str | None = None,
    citations: list | None = None,
    tool_traces: list | None = None,
    memory_refs: list | None = None,
    artifact_refs: list | None = None,
    approval_state: str | None = None,
    runtime_status: str | None = None,
    parent_message_id: str | None = None,
    pinned: bool = False,
) -> dict | None:
    """Save a message and update the conversation timestamp.

    When *_member_id* and *_org_id* are provided the function also fetches
    the conversation row (scoped to that member/org) and returns it as a
    plain dict so callers can hydrate ``project_id`` without an extra query.
    Returns None when those kwargs are absent.
    """
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
                structured_response=structured_response,
                token_count=len(content.split()),
                model=model,
                mode=mode,
                citations=citations,
                tool_traces=tool_traces,
                memory_refs=memory_refs,
                artifact_refs=artifact_refs,
                approval_state=approval_state,
                runtime_status=runtime_status,
                parent_message_id=parent_message_id,
                pinned=pinned,
            )
        )
        await conn.execute(
            update(conversations)
            .where(conversations.c.id == conversation_id)
            .values(updated_at=datetime.now(timezone.utc))
        )
        if _member_id is not None and _org_id is not None:
            row = (
                await conn.execute(
                    select(conversations).where(
                        conversations.c.id == conversation_id,
                        conversations.c.member_id == _member_id,
                        conversations.c.organization_id == _org_id,
                    )
                )
            ).mappings().first()
            return dict(row) if row else None
    return None


def _normalize_traces(raw: list[dict]) -> list[dict]:
    """Merge paired tool_call/tool_result events into single ToolTrace dicts.

    Rules:
    - ``thinking`` events are skipped entirely.
    - ``tool_call`` events are held in a pending dict keyed by tool name.
    - ``tool_result`` / ``tool_error`` events close the matching pending entry,
      producing one output row with status "complete" or "error".
    - Unmatched tool_calls (no result yet) are emitted with status "pending".
    - Non-tool events (e.g. ``sub_agent_spawned``) that have a "tool" field
      are included; otherwise they are skipped.
    - Every output row is guaranteed to have: id, tool, summary, status.
    """
    pending: dict[str, dict] = {}  # tool_name → partial row
    out: list[dict] = []

    for event in raw:
        etype = event.get("type", "")

        if etype == "thinking":
            continue

        if etype == "tool_call":
            tool = event.get("tool", "")
            pending[tool] = {
                "id": str(uuid.uuid4()),
                "tool": tool,
                "summary": event.get("summary", ""),
                "status": "pending",
            }

        elif etype in ("tool_result", "tool_error"):
            tool = event.get("tool", "")
            row = pending.pop(tool, None)
            if row is None:
                row = {"id": str(uuid.uuid4()), "tool": tool, "summary": "", "status": "pending"}
            if etype == "tool_result":
                row["summary"] = event.get("summary", row.get("summary", ""))
                row["status"] = "complete"
            else:
                row["summary"] = event.get("error", event.get("summary", row.get("summary", "")))
                row["status"] = "error"
            out.append(row)

        else:
            # Non-tool events: only include if they have a "tool" field
            tool = event.get("tool")
            if tool:
                out.append({
                    "id": str(uuid.uuid4()),
                    "tool": tool,
                    "summary": event.get("summary", ""),
                    "status": event.get("status", "complete"),
                })

    # Flush any unmatched pending tool_calls
    for row in pending.values():
        out.append(row)

    return out


@router.get("/models")
async def list_chat_models(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_chat_models", settings.org_id)
    return available_chat_models()


@router.get("/modes")
async def list_chat_modes(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_chat_modes", settings.org_id)
    return available_modes()


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
        organization_id=member.organization_id,
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
                    organization_id=org_id,
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
            organization_id=org_id,
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


async def _agent_loop_stream(
    *,
    conversation_id: str,
    goal: str,
    member: Member,
    persona_id: str | None,
    workspace_id: str | None,
    model: str | None,
    original_message: str | None = None,
    requester_context: RequesterContext | None = None,
    user_message_for_memory: str | None = None,
    attachments_context: list[dict] | None = None,
    router_decision: dict | None = None,
    conversation_context: list[dict] | None = None,
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
        attachments_context=attachments_context,
        original_message=original_message,
        router_decision=router_decision,
        conversation_context=conversation_context,
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
                _sr = event.get("structured_response")
                if _sr is not None:
                    yield f"data: {json.dumps({'type': 'structured_response', 'structured_response': _sr})}\n\n"
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
            _spawn_background(
                extract_and_save(conversation_id, user_message_for_memory, final_answer, requester_context),
                label=f"extract_and_save:{conversation_id}",
            )

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


#: Per-image byte cap for inline base64 vision blocks. base64 inflates bytes ~33%,
#: and providers bound the request body (OpenAI ~20MB), so a 25MB upload would blow
#: the limit and fail opaquely at the provider. 5MB/image keeps us well inside it.
_MAX_IMAGE_BYTES_VISION = 5 * 1024 * 1024


async def _build_vision_blocks(
    attachment_ids: list[str], org_id: str, model_id: str
) -> tuple[list[dict], bool]:
    """Resolve org-owned image attachments into vision content blocks.

    Returns ``(image_blocks, has_images)`` where ``has_images`` is True if any
    org-owned image attachment was present (even when no block was built — e.g. the
    model lacks vision, or an image exceeded the size cap), so callers can choose the
    honest degraded path. Cross-org or missing artifacts are skipped (tenant boundary).
    Oversized images are skipped rather than embedded to avoid provider body-limit
    failures. ``_get_artifact``/``_read_artifact_content`` are module globals so tests
    can inject fixtures.
    """
    image_blocks: list[dict] = []
    has_images = False
    supports_vision = model_supports_vision(model_id)
    for att_id in attachment_ids:
        meta = await _get_artifact(att_id)
        if not meta or str(meta.get("organization_id")) != str(org_id):
            continue  # cross-org or missing — skip (security)
        mime = str(meta.get("mime_type") or "")
        if mime not in _IMAGE_MIMES:
            continue
        has_images = True
        if supports_vision:
            raw_bytes = await _read_artifact_content(att_id) or b""
            if raw_bytes and len(raw_bytes) <= _MAX_IMAGE_BYTES_VISION:
                image_blocks.append(build_image_block(raw_bytes, mime))
    return image_blocks, has_images


@router.post("/message")
async def send_message(req: ChatRequest, member: Member = Depends(get_current_member)) -> StreamingResponse:
    from fastapi import HTTPException

    await permissions.check(member, "chat", req.conversation_id or "new_conversation")
    try:
        selected_model = normalize_chat_model(req.model)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ── Build artifact_refs for user message (from attachment metadata) ────────
    # Resolve attachment metadata up-front so it can be persisted on the user
    # message row. Only attachments belonging to this org are included.
    _attachment_ids_raw: list[str] = getattr(req, "attachment_ids", []) or []
    _user_artifact_refs: list[dict] = []
    for _att_id in _attachment_ids_raw:
        _meta = await _get_artifact(_att_id)
        if _meta and str(_meta.get("organization_id")) == str(member.organization_id):
            _user_artifact_refs.append({
                "id": _att_id,
                "title": _meta.get("title") or "attachment",
                "kind": _meta.get("kind") or "attachment",
                "mime_type": _meta.get("mime_type"),
                "size_bytes": _meta.get("size_bytes"),
            })

    # ── Conversation creation / project_id hydration ────────────────────────
    if req.conversation_id:
        # Existing conversation: save user message and fetch conversation row
        # (single round-trip) so we can hydrate project_id without an extra query.
        conv_row = await _save_message(
            req.conversation_id, "user", req.message,
            artifact_refs=_user_artifact_refs or None,
            _member_id=member.id, _org_id=member.organization_id,
        )
        conversation_id = req.conversation_id
        # Validate / hydrate project_id from the stored conversation row.
        stored_project_id = conv_row.get("project_id") if conv_row else None
        if req.project_id is not None and stored_project_id is not None and req.project_id != stored_project_id:
            raise HTTPException(
                status_code=422,
                detail="project_id does not match conversation",
            )
        effective_project_id = req.project_id if req.project_id is not None else stored_project_id
    else:
        # New conversation: create it (with project_id if supplied), then save
        # the user message WITHOUT a RETURNING fetch (_member_id omitted).
        conversation_id = await _create_conversation(member, req.message, project_id=req.project_id)
        await _save_message(
            conversation_id, "user", req.message,
            artifact_refs=_user_artifact_refs or None,
        )
        effective_project_id = req.project_id

    requester_context = RequesterContext.from_member(member)
    requester_context.persona_id = req.persona_id
    requester_context.workspace_id = req.workspace_id
    requester_context.project_id = effective_project_id
    explicit_memory = extract_explicit_memory_content(req.message)

    if explicit_memory and not _attachment_ids_raw:
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
            await audit.log("chat_response", member.id, "chat.message", organization_id=member.organization_id, resource_id=conversation_id)
            yield f"data: {json.dumps({'type': 'conversation', 'conversation_id': conversation_id})}\n\n"
            yield f"data: {json.dumps({'type': 'memory_saved', 'entry_id': entry_id, 'content': explicit_memory, 'scope': 'org', 'source': 'explicit'})}\n\n"
            yield f"data: {json.dumps({'type': 'token', 'content': assistant_response})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(explicit_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    attachments_context: list[dict] = []
    if _attachment_ids_raw:
        attachments_context = await _parse_attachments(_attachment_ids_raw, conversation_id, member.organization_id)

    # classify_intent and assemble_context are independent, and both the task and
    # chat branches need the assembled context — so run them concurrently to overlap
    # their LLM round-trips instead of paying for them in series. Obviously
    # conversational messages skip the classifier LLM call entirely.
    if _is_trivial_chat(req.message):
        intent = {"mode": "chat", "goal": None}
        context = await assemble_context(conversation_id, req.message, requester_context)
    else:
        intent, context = await asyncio.gather(
            classify_intent(req.message),
            assemble_context(conversation_id, req.message, requester_context),
        )

    # Explicit tasks stay durable immediately. Ordinary chat, including non-trivial
    # tool-using chat, goes through stream_chat_turn below so conversation history
    # is assembled before any lazy task/tool execution starts.
    if intent["mode"] == "task":
        return StreamingResponse(
            _agent_loop_stream(
                conversation_id=conversation_id,
                goal=intent.get("goal") or req.message,
                member=member,
                persona_id=req.persona_id,
                workspace_id=req.workspace_id,
                model=selected_model,
                original_message=req.message,
                router_decision={
                    "mode": "agent",
                    "ui_title": intent.get("goal") or req.message,
                    "metadata": {"classifier": intent},
                },
                requester_context=None,
                user_message_for_memory=None,
                attachments_context=attachments_context or None,
                conversation_context=context[:-1],
            ),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    async def _sse_wrap(gen):
        async for event in gen:
            yield f"data: {json.dumps(event)}\n\n"

    # ── Vision / multimodal user-turn assembly (chat path only) ──────────────
    # Resolved here, after the task-mode return, so this org-scoped artifact I/O and
    # base64 work runs only for the chat path that actually consumes it. The helper
    # enforces the tenant boundary and the per-image size cap.
    _image_blocks, _has_images = await _build_vision_blocks(
        _attachment_ids_raw, member.organization_id, selected_model
    )
    _vision_active = bool(_image_blocks)

    # Degraded path: embed OCR-extracted text from already-parsed image attachments so
    # the model actually receives the image content. The honest "vision unavailable"
    # note is only attached when extracted text exists — otherwise the note would
    # overclaim (an image with no extractable text would carry a false promise).
    _ocr_text: str | None = None
    if _has_images and not _vision_active and attachments_context:
        _ocr_parts = [
            f"[Image: {entry.get('filename') or 'attachment'}]\n{entry['preview']}"
            for entry in attachments_context
            if entry.get("preview")
        ]
        if _ocr_parts:
            _ocr_text = "\n\n".join(_ocr_parts)

    _user_content = build_user_turn_content(
        req.message,
        _image_blocks,
        vision_available=_vision_active,
        ocr_text=_ocr_text,
        ocr_note=_VISION_UNAVAILABLE_NOTE if (_has_images and not _vision_active and _ocr_text) else None,
    )

    return StreamingResponse(
        _sse_wrap(
            stream_chat_turn(
                conversation_id=conversation_id,
                message=req.message,
                context_messages=context[:-1],  # exclude the appended user message
                requester_context=requester_context,
                model=selected_model,
                mode=req.mode,
                user_content=_user_content,
            )
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# ─── Per-message controls ──────────────────────────────────────────────────────

class EditMessageRequest(BaseModel):
    content: str


class SaveToMemoryRequest(BaseModel):
    scope: str = "org"


class ConvertToTaskRequest(BaseModel):
    model: str | None = None
    mode: str | None = None


class ConvertToWorkflowRequest(BaseModel):
    name: str | None = None
    workspace_id: str = "default"
    employee_id: str | None = None


class RetryMessageRequest(BaseModel):
    model: str | None = None
    mode: str | None = None


def _retry_payload_for_message(rows: list[dict], message_id: str) -> dict[str, Any]:
    """Build the replay payload for regenerate/retry actions.

    User-message retry replays that user turn with only earlier messages as
    context. Assistant-message regenerate finds the nearest previous user turn
    and replays it with context before that turn.
    """
    target_idx = next((idx for idx, row in enumerate(rows) if str(row.get("id")) == message_id), None)
    if target_idx is None:
        raise HTTPException(status_code=404, detail="Message not found")

    target = rows[target_idx]
    if target.get("role") == "assistant":
        user_idx = next(
            (idx for idx in range(target_idx - 1, -1, -1) if rows[idx].get("role") == "user"),
            None,
        )
        if user_idx is None:
            raise HTTPException(status_code=400, detail="No prior user message to regenerate")
        target_idx = user_idx
        target = rows[user_idx]
    elif target.get("role") != "user":
        raise HTTPException(status_code=400, detail="Only user or assistant messages can be retried")

    return {
        "message": str(target.get("content") or ""),
        "context_messages": [
            {"role": str(row.get("role")), "content": str(row.get("content") or "")}
            for row in rows[:target_idx]
            if row.get("role") in {"user", "assistant"} and row.get("content") is not None
        ],
        "source_message_id": str(target.get("id")),
    }


async def _check_conversation_ownership(
    conn: Any,
    conversations: Any,
    conversation_id: str,
    member: Member,
) -> dict:
    """Return conversation row or raise 404 if not owned by member/org."""
    from fastapi import HTTPException
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


@router.post("/conversations/{conversation_id}/messages/{message_id}/pin")
async def pin_message(
    conversation_id: str,
    message_id: str,
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "pin_message", conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        await _check_conversation_ownership(conn, conversations, conversation_id, member)
        await conn.execute(
            update(messages)
            .where(messages.c.id == message_id, messages.c.conversation_id == conversation_id)
            .values(pinned=True)
        )
    await audit.log(
        "message_pinned",
        member.id,
        "chat.pin_message",
        organization_id=member.organization_id,
        resource_type="messages",
        resource_id=message_id,
    )
    return {"pinned": True}


@router.post("/conversations/{conversation_id}/messages/{message_id}/unpin")
async def unpin_message(
    conversation_id: str,
    message_id: str,
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "unpin_message", conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        await _check_conversation_ownership(conn, conversations, conversation_id, member)
        await conn.execute(
            update(messages)
            .where(messages.c.id == message_id, messages.c.conversation_id == conversation_id)
            .values(pinned=False)
        )
    await audit.log(
        "message_unpinned",
        member.id,
        "chat.unpin_message",
        organization_id=member.organization_id,
        resource_type="messages",
        resource_id=message_id,
    )
    return {"pinned": False}


@router.patch("/conversations/{conversation_id}/messages/{message_id}")
async def edit_message(
    conversation_id: str,
    message_id: str,
    req: EditMessageRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    from fastapi import HTTPException
    await permissions.check(member, "edit_message", conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        await _check_conversation_ownership(conn, conversations, conversation_id, member)
        msg_row = (
            await conn.execute(
                select(messages).where(
                    messages.c.id == message_id,
                    messages.c.conversation_id == conversation_id,
                )
            )
        ).mappings().first()
        if msg_row is None:
            raise HTTPException(status_code=404, detail="Message not found")
        if msg_row["role"] != "user":
            raise HTTPException(status_code=400, detail="Only user messages can be edited")
        prev_content = msg_row["content"]
        await conn.execute(
            update(messages)
            .where(messages.c.id == message_id)
            .values(content=req.content)
        )
    await audit.log(
        "message_edited",
        member.id,
        "chat.edit_message",
        organization_id=member.organization_id,
        resource_type="messages",
        resource_id=message_id,
        payload={"prev_length": len(prev_content), "new_length": len(req.content)},
    )
    return {"content": req.content}


@router.post("/conversations/{conversation_id}/messages/{message_id}/branch")
async def branch_conversation(
    conversation_id: str,
    message_id: str,
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "branch_conversation", conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        conv_row = await _check_conversation_ownership(conn, conversations, conversation_id, member)
        # Fetch the branch point message
        src_msg = (
            await conn.execute(
                select(messages).where(
                    messages.c.id == message_id,
                    messages.c.conversation_id == conversation_id,
                )
            )
        ).mappings().first()
        if src_msg is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Message not found")
        src_msg = dict(src_msg)
        # Fetch all messages up to and including the branch point using or_
        prior_msgs = (
            await conn.execute(
                select(messages).where(
                    or_(
                        messages.c.id == message_id,
                        messages.c.created_at < src_msg["created_at"],
                        # Deterministic tie-break when timestamps collide. UUID ids
                        # aren't time-ordered, but this keeps selection stable.
                        (messages.c.created_at == src_msg["created_at"])
                        & (messages.c.id <= message_id),
                    ),
                    messages.c.conversation_id == conversation_id,
                ).order_by(messages.c.created_at.asc())
            )
        ).mappings().all()
        # Create new conversation
        new_conv_result = await conn.execute(
            insert(conversations).values(
                organization_id=member.organization_id,
                region=settings.region,
                member_id=member.id,
                title=f"Branch: {conv_row.get('title', '')}",
            ).returning(conversations.c.id)
        )
        new_conv_id = str(new_conv_result.scalar_one())
        # Copy messages into new conversation
        if prior_msgs:
            for msg in prior_msgs:
                msg_dict = dict(msg)
                msg_dict.pop("id", None)
                msg_dict["conversation_id"] = new_conv_id
                msg_dict["organization_id"] = member.organization_id
                await conn.execute(insert(messages).values(**msg_dict))
    await audit.log(
        "conversation_branched",
        member.id,
        "chat.branch_conversation",
        organization_id=member.organization_id,
        resource_type="conversations",
        resource_id=new_conv_id,
        payload={"source_conversation_id": conversation_id, "branch_message_id": message_id},
    )
    return {"conversation_id": new_conv_id}


@router.post("/conversations/{conversation_id}/messages/{message_id}/save-to-memory")
async def save_message_to_memory(
    conversation_id: str,
    message_id: str,
    req: SaveToMemoryRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "save_to_memory", conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        await _check_conversation_ownership(conn, conversations, conversation_id, member)
        msg_row = (
            await conn.execute(
                select(messages).where(
                    messages.c.id == message_id,
                    messages.c.conversation_id == conversation_id,
                )
            )
        ).mappings().first()
        if msg_row is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Message not found")
        msg_row = dict(msg_row)
    scope_id = member.id if req.scope == "personal" else member.organization_id
    requester_context = RequesterContext.from_member(member)
    entry_id = await create_memory_entry(
        content=msg_row["content"],
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
        organization_id=member.organization_id,
        resource_type="messages",
        resource_id=message_id,
        payload={"scope": req.scope, "memory_entry_id": str(entry_id)},
    )
    return {"memory_entry_id": entry_id}


@router.post("/conversations/{conversation_id}/messages/{message_id}/convert-to-task")
async def convert_message_to_task(
    conversation_id: str,
    message_id: str,
    req: ConvertToTaskRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "convert_to_task", conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        await _check_conversation_ownership(conn, conversations, conversation_id, member)
        msg_row = (
            await conn.execute(
                select(messages).where(
                    messages.c.id == message_id,
                    messages.c.conversation_id == conversation_id,
                )
            )
        ).mappings().first()
        if msg_row is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Message not found")
        msg_row = dict(msg_row)
    task_id = await create_task_record(
        goal=msg_row["content"],
        member=member,
        triggered_by=conversation_id,
        model=req.model,
        mode=req.mode,
    )
    await audit.log(
        "message_converted_to_task",
        member.id,
        "chat.convert_message_to_task",
        organization_id=member.organization_id,
        resource_type="messages",
        resource_id=message_id,
        payload={"task_id": str(task_id), "conversation_id": conversation_id},
    )
    return {"task_id": task_id}


@router.post("/conversations/{conversation_id}/messages/{message_id}/convert-to-workflow")
async def convert_message_to_workflow(
    conversation_id: str,
    message_id: str,
    req: ConvertToWorkflowRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "convert_to_workflow", conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        await _check_conversation_ownership(conn, conversations, conversation_id, member)
        msg_row = (
            await conn.execute(
                select(messages).where(
                    messages.c.id == message_id,
                    messages.c.conversation_id == conversation_id,
                )
            )
        ).mappings().first()
        if msg_row is None:
            raise HTTPException(status_code=404, detail="Message not found")
        msg_row = dict(msg_row)
    repo = await workflow_repository()
    workflow = await workflow_runtime(repo).create_workflow(
        tenant_id=member.organization_id,
        workspace_id=req.workspace_id,
        employee_id=req.employee_id or member.id,
        user_id=member.id,
        name=req.name or f"Workflow from message {message_id[:8]}",
        description=f"Created from chat message {message_id} in conversation {conversation_id}.",
        steps=[
            {
                "id": "message_input",
                "tool_name": "internal_echo__echo",
                "arguments": {"message": msg_row["content"]},
                "max_attempts": 1,
                "parallel_safe": False,
            }
        ],
    )
    await audit.log(
        "message_converted_to_workflow",
        member.id,
        "chat.convert_message_to_workflow",
        organization_id=member.organization_id,
        resource_type="messages",
        resource_id=message_id,
        payload={"workflow_id": str(workflow["id"]), "conversation_id": conversation_id},
    )
    return {"workflow_id": workflow["id"]}


async def _replay_message_turn(
    *,
    conversation_id: str,
    message_id: str,
    req: RetryMessageRequest,
    member: Member,
    action: str,
) -> dict:
    await permissions.check(member, action, conversation_id)
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        await _check_conversation_ownership(conn, conversations, conversation_id, member)
        rows = (
            await conn.execute(
                select(messages)
                .where(messages.c.conversation_id == conversation_id)
                .order_by(messages.c.created_at.asc(), messages.c.id.asc())
            )
        ).mappings().all()
    payload = _retry_payload_for_message([dict(row) for row in rows], message_id)
    requester_context = RequesterContext.from_member(member)
    output = ""
    task_id: str | None = None
    async for event in stream_chat_turn(
        conversation_id=conversation_id,
        message=payload["message"],
        context_messages=payload["context_messages"],
        requester_context=requester_context,
        model=req.model,
        mode=req.mode,
        emit_conversation=False,
    ):
        if event.get("type") == "token":
            output += str(event.get("content") or "")
        elif event.get("type") == "task_created":
            task_id = str(event.get("task_id"))
    await audit.log(
        "message_regenerated" if action == "regenerate_message" else "message_retried",
        member.id,
        f"chat.{action}",
        organization_id=member.organization_id,
        resource_type="messages",
        resource_id=message_id,
        payload={"source_message_id": payload["source_message_id"], "task_id": task_id},
    )
    return {"conversation_id": conversation_id, "source_message_id": payload["source_message_id"], "content": output, "task_id": task_id}


@router.post("/conversations/{conversation_id}/messages/{message_id}/regenerate")
async def regenerate_message(
    conversation_id: str,
    message_id: str,
    req: RetryMessageRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    return await _replay_message_turn(
        conversation_id=conversation_id,
        message_id=message_id,
        req=req,
        member=member,
        action="regenerate_message",
    )


@router.post("/conversations/{conversation_id}/messages/{message_id}/retry-from-here")
async def retry_message_from_here(
    conversation_id: str,
    message_id: str,
    req: RetryMessageRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    return await _replay_message_turn(
        conversation_id=conversation_id,
        message_id=message_id,
        req=req,
        member=member,
        action="retry_message",
    )
