import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.sql import func

from core import permissions
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.embeddings import embed
from core.memory_writes import create_memory_entry, undo_autonomous_memory, vector_literal
from core.models import Member, RequesterContext
from core.redis import redis_client

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryCreate(BaseModel):
    content: str
    scope: str = "org"
    scope_id: str | None = None
    importance_score: float = 0.8


class MemoryUpdate(BaseModel):
    content: str


@router.get("/")
async def list_memory(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await permissions.check(member, "list_memory", settings.org_id)
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    memory_entries.c.id,
                    memory_entries.c.scope,
                    memory_entries.c.scope_id,
                    memory_entries.c.content,
                    memory_entries.c.source,
                    memory_entries.c.importance_score,
                    memory_entries.c.created_by,
                    memory_entries.c.created_at,
                    memory_entries.c.updated_at,
                )
                .where(
                    memory_entries.c.organization_id == member.organization_id,
                    memory_entries.c.is_deleted.is_(False),
                )
                .order_by(memory_entries.c.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


@router.post("/")
async def add_memory(req: MemoryCreate, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "create_memory", settings.org_id)
    entry_id = await create_memory_entry(
        content=req.content,
        requester_context=RequesterContext.from_member(member),
        source="explicit",
        scope=req.scope,
        scope_id=req.scope_id or member.organization_id,
        importance_score=req.importance_score,
        created_by=member.id,
    )
    return {"id": entry_id}


@router.patch("/{memory_id}")
async def update_memory(memory_id: str, req: MemoryUpdate, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "update_memory", memory_id)
    vector = await embed(req.content)
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        result = await conn.execute(
            update(memory_entries)
            .where(
                memory_entries.c.id == memory_id,
                memory_entries.c.organization_id == member.organization_id,
                memory_entries.c.is_deleted.is_(False),
            )
            .values(content=req.content, embedding=vector_literal(vector), updated_at=func.now())
            .returning(memory_entries.c.id)
        )
        updated = result.scalar_one_or_none()
    if updated is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    await audit.log(
        "memory_write",
        member.id,
        "memory.update",
        resource_type="memory_entries",
        resource_id=memory_id,
    )
    return {"id": memory_id}


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "delete_memory", memory_id)
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        result = await conn.execute(
            update(memory_entries)
            .where(
                memory_entries.c.id == memory_id,
                memory_entries.c.organization_id == member.organization_id,
                memory_entries.c.is_deleted.is_(False),
            )
            .values(is_deleted=True, updated_at=func.now())
            .returning(memory_entries.c.id)
        )
        deleted = result.scalar_one_or_none()
    if deleted is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    await audit.log(
        "memory_write",
        member.id,
        "memory.delete",
        resource_type="memory_entries",
        resource_id=memory_id,
    )
    return {"id": memory_id, "deleted": True}


@router.post("/{memory_id}/undo")
async def undo_memory(memory_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "undo_memory", memory_id)
    undone = await undo_autonomous_memory(memory_id, member)
    if not undone:
        raise HTTPException(status_code=404, detail="Undo window expired or memory not found")
    return {"id": memory_id, "undone": True}


@router.get("/events/{conversation_id}")
async def memory_events(conversation_id: str, member: Member = Depends(get_current_member)) -> StreamingResponse:
    await permissions.check(member, "stream_memory_events", conversation_id)

    async def stream():
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(f"memories:{conversation_id}")
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                if message and message.get("type") == "message":
                    payload = message["data"]
                    if not isinstance(payload, str):
                        payload = json.dumps(payload)
                    yield f"data: {payload}\n\n"
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0)
        finally:
            await pubsub.unsubscribe(f"memories:{conversation_id}")
            await pubsub.close()

    return StreamingResponse(stream(), media_type="text/event-stream")
