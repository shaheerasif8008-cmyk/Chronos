import asyncio
import csv
import io
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.memory_writes import (
    archive_memory_entry,
    create_memory_entry,
    detect_conflicting_memory,
    list_memory_access_logs,
    list_memory_records,
    merge_memory_entries,
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
    confidence_score: float = 1.0
    source: str = "explicit"
    is_sensitive: bool = False
    provenance: dict[str, Any] | None = None


class MemoryUpdate(BaseModel):
    content: str
    importance_score: float | None = None
    confidence_score: float | None = None
    scope: str | None = None
    scope_id: str | None = None
    status: str | None = None
    is_pinned: bool | None = None
    is_archived: bool | None = None
    is_sensitive: bool | None = None
    staleness: str | None = None
    provenance: dict[str, Any] | None = None
    conflict_group_id: str | None = None


class MemoryMerge(BaseModel):
    primary_id: str
    merged_ids: list[str] = []
    content: str | None = None


class MemoryImport(BaseModel):
    memories: list[MemoryCreate]


def _scope_id_for(scope: str, member: Member, requested: str | None) -> str:
    if requested:
        return requested
    if scope in {"personal", "restricted"}:
        return member.id
    if scope in {"org", "workspace"}:
        return member.organization_id
    raise HTTPException(status_code=400, detail=f"{scope} memory requires scope_id")


@router.get("/")
async def list_memory(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    include_archived: bool = False,
    query: str | None = None,
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await permissions.check(member, "list_memory", settings.org_id)
    return await list_memory_records(member, limit=limit, offset=offset, include_archived=include_archived, query=query)


@router.post("/")
async def add_memory(req: MemoryCreate, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "create_memory", settings.org_id)
    scope_id = _scope_id_for(req.scope, member, req.scope_id)
    entry_id = await create_memory_entry(
        content=req.content,
        requester_context=RequesterContext.from_member(member),
        source=req.source,
        scope=req.scope,
        scope_id=scope_id,
        importance_score=req.importance_score,
        confidence_score=req.confidence_score,
        is_sensitive=req.is_sensitive,
        provenance=req.provenance or {"source": "memory_control_center"},
        created_by=member.id,
    )
    conflicts = await detect_conflicting_memory(req.content, member, exclude_id=entry_id)
    return {"id": entry_id, "conflicts": conflicts}


@router.patch("/{memory_id}")
async def update_memory(memory_id: str, req: MemoryUpdate, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "update_memory", memory_id)
    if not await update_memory_entry(
        memory_id,
        req.content,
        member,
        importance_score=req.importance_score,
        confidence_score=req.confidence_score,
        scope=req.scope,
        scope_id=req.scope_id,
        status=req.status,
        is_pinned=req.is_pinned,
        is_archived=req.is_archived,
        is_sensitive=req.is_sensitive,
        staleness=req.staleness,
        provenance=req.provenance,
        conflict_group_id=req.conflict_group_id,
    ):
        raise HTTPException(status_code=404, detail="Memory not found")
    await audit.log(
        "memory_write",
        member.id,
        "memory.update",
        resource_type="memory",
        resource_id=memory_id,
    )
    return {"id": memory_id}


@router.post("/{memory_id}/archive")
async def archive_memory(memory_id: str, archived: bool = True, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "archive_memory", memory_id)
    if not await archive_memory_entry(memory_id, member, archived=archived):
        raise HTTPException(status_code=404, detail="Memory not found")
    await audit.log("memory_write", member.id, "memory.archive", resource_type="memory", resource_id=memory_id, payload={"archived": archived})
    return {"id": memory_id, "archived": archived}


@router.post("/merge")
async def merge_memory(req: MemoryMerge, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "merge_memory", req.primary_id)
    if not await merge_memory_entries(req.primary_id, req.merged_ids, member, content=req.content):
        raise HTTPException(status_code=404, detail="Primary memory not found")
    return {"id": req.primary_id, "merged_ids": req.merged_ids}


@router.get("/{memory_id}/access-logs")
async def memory_access_logs(memory_id: str, limit: int = Query(default=50, ge=1, le=200), member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "list_memory_access_logs", memory_id)
    return await list_memory_access_logs(memory_id, member, limit=limit)


@router.post("/import")
async def import_memory(req: MemoryImport, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "import_memory", settings.org_id)
    created: list[str] = []
    for item in req.memories:
        scope_id = _scope_id_for(item.scope, member, item.scope_id)
        created.append(
            await create_memory_entry(
                content=item.content,
                requester_context=RequesterContext.from_member(member),
                source=item.source if item.source != "explicit" else "imported",
                scope=item.scope,
                scope_id=scope_id,
                importance_score=item.importance_score,
                confidence_score=item.confidence_score,
                is_sensitive=item.is_sensitive,
                provenance=item.provenance or {"source": "memory_import"},
                created_by=member.id,
            )
        )
    await audit.log("memory_write", member.id, "memory.import", resource_type="memory", payload={"count": len(created)})
    return {"created": created}


@router.get("/export.csv")
async def export_memory(member: Member = Depends(get_current_member)) -> StreamingResponse:
    await permissions.check(member, "export_memory", settings.org_id)
    rows = await list_memory_records(member, limit=1000, include_archived=True)
    handle = io.StringIO()
    fields = ["id", "scope", "scope_id", "content", "source", "importance_score", "confidence_score", "status", "is_pinned", "is_archived", "is_sensitive", "staleness", "created_by", "created_at"]
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key in fields})
    handle.seek(0)
    await audit.log("memory_export", member.id, "memory.export", resource_type="memory", payload={"count": len(rows)})
    return StreamingResponse(iter([handle.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=chronos-memory.csv"})


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
