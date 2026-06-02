from __future__ import annotations
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.memory_control import (
    archive_memory,
    change_scope,
    detect_conflicts,
    export_memories,
    import_memories,
    list_memories,
    list_memory_usage,
    merge_memories,
    resolve_conflict,
    set_memory_policy,
    set_pinned,
    set_sensitive,
)
from core.memory_writes import (
    create_memory_entry,
    soft_delete_memory_entry,
    undo_autonomous_memory,
    update_memory_entry,
)
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
    importance_score: float | None = None


class FlagBody(BaseModel):
    value: bool = True


class ScopeBody(BaseModel):
    scope: str
    scope_id: str


class MergeBody(BaseModel):
    primary_id: str
    duplicate_ids: list[str]


class ResolveConflictBody(BaseModel):
    stale_id: str
    survivor_id: str


class PolicyBody(BaseModel):
    scope: str
    scope_id: str
    enabled: bool


class ImportBody(BaseModel):
    items: list[dict]


@router.get("/")
async def list_memory(
    q: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
    include_superseded: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await permissions.check(member, "list_memory", settings.org_id)
    return await list_memories(
        member,
        query=q,
        scope=scope,
        include_archived=include_archived,
        include_superseded=include_superseded,
        limit=limit,
        offset=offset,
    )


@router.post("/")
async def add_memory(req: MemoryCreate, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "create_memory", settings.org_id)
    from core.memory_control import is_memory_enabled

    if not await is_memory_enabled(org_id=member.organization_id, member_id=member.id):
        raise HTTPException(status_code=403, detail="Memory is disabled for this scope")
    scope_id = req.scope_id
    if scope_id is None:
        scope_id = member.id if req.scope in {"personal", "restricted"} else member.organization_id
    entry_id = await create_memory_entry(
        content=req.content,
        requester_context=RequesterContext.from_member(member),
        source="explicit",
        scope=req.scope,
        scope_id=scope_id,
        importance_score=req.importance_score,
        created_by=member.id,
    )
    return {"id": entry_id}


@router.patch("/{memory_id}")
async def update_memory(memory_id: str, req: MemoryUpdate, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "update_memory", memory_id)
    if not await update_memory_entry(memory_id, req.content, member, importance_score=req.importance_score):
        raise HTTPException(status_code=404, detail="Memory not found")
    await audit.log(
        "memory_write",
        member.id,
        "memory.update",
        resource_type="memory",
        resource_id=memory_id,
    )
    return {"id": memory_id}


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "delete_memory", memory_id)
    if not await soft_delete_memory_entry(memory_id, member):
        raise HTTPException(status_code=404, detail="Memory not found")
    await audit.log(
        "memory_write",
        member.id,
        "memory.delete",
        resource_type="memory",
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


@router.get("/conflicts")
async def memory_conflicts(member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "list_memory", settings.org_id)
    return await detect_conflicts(member)


@router.post("/resolve-conflict")
async def memory_resolve_conflict(req: ResolveConflictBody, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "update_memory", req.stale_id)
    if not await resolve_conflict(member, stale_id=req.stale_id, survivor_id=req.survivor_id):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"resolved": True, "stale_id": req.stale_id}


@router.post("/merge")
async def memory_merge(req: MergeBody, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "update_memory", req.primary_id)
    count = await merge_memories(member, primary_id=req.primary_id, duplicate_ids=req.duplicate_ids)
    if count == 0:
        raise HTTPException(status_code=404, detail="No duplicates merged (primary not found or empty set)")
    return {"primary_id": req.primary_id, "superseded": count}


@router.get("/export")
async def memory_export(member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "list_memory", settings.org_id)
    return await export_memories(member)


@router.post("/import")
async def memory_import(req: ImportBody, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "create_memory", settings.org_id)
    ids = await import_memories(member, req.items)
    return {"imported": len(ids), "ids": ids}


@router.post("/policy")
async def memory_policy(req: PolicyBody, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "update_memory", f"{req.scope}:{req.scope_id}")
    try:
        await set_memory_policy(member, scope=req.scope, scope_id=req.scope_id, enabled=req.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"scope": req.scope, "scope_id": req.scope_id, "enabled": req.enabled}


@router.get("/{memory_id}/usage")
async def memory_usage(memory_id: str, member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "list_memory", memory_id)
    return await list_memory_usage(memory_id, member)


@router.post("/{memory_id}/archive")
async def memory_archive(memory_id: str, req: FlagBody, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "update_memory", memory_id)
    if not await archive_memory(memory_id, member, archived=req.value):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"id": memory_id, "is_archived": req.value}


@router.post("/{memory_id}/pin")
async def memory_pin(memory_id: str, req: FlagBody, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "update_memory", memory_id)
    if not await set_pinned(memory_id, member, pinned=req.value):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"id": memory_id, "is_pinned": req.value}


@router.post("/{memory_id}/sensitive")
async def memory_sensitive(memory_id: str, req: FlagBody, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "update_memory", memory_id)
    if not await set_sensitive(memory_id, member, sensitive=req.value):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"id": memory_id, "is_sensitive": req.value}


@router.post("/{memory_id}/scope")
async def memory_change_scope(memory_id: str, req: ScopeBody, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "update_memory", memory_id)
    if not await change_scope(memory_id, member, scope=req.scope, scope_id=req.scope_id):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"id": memory_id, "scope": req.scope, "scope_id": req.scope_id}


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
