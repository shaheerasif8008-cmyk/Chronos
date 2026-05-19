from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import insert, select, update
from sqlalchemy.sql import func

from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.embeddings import embed
from core.models import Member

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryCreate(BaseModel):
    content: str
    scope: str = "org"
    scope_id: str | None = None
    importance_score: float = 0.8


class MemoryUpdate(BaseModel):
    content: str


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"


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
    vector = await embed(req.content)
    memory_entries = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(memory_entries)
            .values(
                organization_id=member.organization_id,
                region=member.region,
                scope=req.scope,
                scope_id=req.scope_id or member.organization_id,
                content=req.content,
                embedding=_vector_literal(vector),
                source="explicit",
                importance_score=req.importance_score,
                created_by=member.id,
            )
            .returning(memory_entries.c.id)
        )
        entry_id = str(result.scalar_one())
    await audit.log(
        "memory_write",
        member.id,
        "memory.create",
        resource_type="memory_entries",
        resource_id=entry_id,
        payload={"source": "explicit", "scope": req.scope},
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
            .values(content=req.content, embedding=_vector_literal(vector), updated_at=func.now())
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
