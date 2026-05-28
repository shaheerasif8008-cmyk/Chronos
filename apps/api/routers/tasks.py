from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import insert, select

from core import permissions
from core.activity_events import list_task_events
from core.modes import normalize_mode
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from core.redis import redis_client
from runtime.executor import TaskExecutor, activity_channel

router = APIRouter(prefix="/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    goal: str
    conversation_id: str | None = None
    persona_id: str | None = None
    workspace_id: str | None = None
    model: str | None = None
    mode: str | None = None
    project_id: str | None = None


async def create_task_record(
    *,
    goal: str,
    member: Member,
    triggered_by: str,
    persona_id: str | None = None,
    workspace_id: str | None = None,
    model: str | None = None,
    mode: str | None = None,
    project_id: str | None = None,
) -> str:
    """Insert a task row. The native model action loop orchestrates by default.

    `model` is the chat-model id chosen in the UI; it is resolved to a concrete
    litellm model string and stored in agent_state so the loop honours the picker.
    """
    from core.llm import resolve_agent_model

    await permissions.check(member, "create_task", workspace_id or "default")
    resolved_model = resolve_agent_model(model)
    normalized_mode = normalize_mode(mode)
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        insert_values: dict = dict(
            organization_id=settings.org_id,
            region=settings.region,
            persona_id=persona_id,
            workspace_id=workspace_id,
            triggered_by=triggered_by,
            triggered_by_member_id=member.id,
            status="pending",
            goal=goal,
            plan={},
            agent_state={"agent_history": [], "iteration_count": 0, "model": resolved_model},
            current_step=0,
            result={},
            depth=0,
            mode=normalized_mode,
        )
        if project_id is not None:
            insert_values["project_id"] = project_id
        result = await conn.execute(
            insert(tasks)
            .values(**insert_values)
            .returning(tasks.c.id)
        )
        return str(result.scalar_one())


@router.post("/")
async def create_task(req: CreateTaskRequest, member: Member = Depends(get_current_member)) -> dict:
    task_id = await create_task_record(
        goal=req.goal,
        member=member,
        triggered_by=req.conversation_id or "manual",
        persona_id=req.persona_id,
        workspace_id=req.workspace_id,
        model=req.model,
        mode=req.mode,
        project_id=req.project_id,
    )
    asyncio.create_task(TaskExecutor().run(task_id))
    return {"task_id": task_id}


@router.get("/")
async def list_tasks(
    status: str | None = Query(default=None),
    include_children: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await permissions.check(member, "list_tasks", settings.org_id)
    tasks = await reflect_table("tasks")
    stmt = select(tasks).where(tasks.c.organization_id == member.organization_id)
    if not include_children:
        stmt = stmt.where(tasks.c.parent_task_id.is_(None))
    if status:
        stmt = stmt.where(tasks.c.status == status)
    stmt = stmt.order_by(tasks.c.created_at.desc()).limit(limit).offset(offset)
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(row) for row in rows]


@router.get("/{task_id}")
async def get_task_detail(task_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "view_task", task_id)
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return dict(row)


@router.get("/{task_id}/events")
async def get_task_events(
    task_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await permissions.check(member, "view_task_events", task_id)
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(tasks.c.id).where(tasks.c.id == task_id, tasks.c.organization_id == member.organization_id)
            )
        ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return await list_task_events(task_id, member.organization_id, limit=limit, offset=offset)


@router.get("/{task_id}/stream")
async def stream_task(task_id: str, member: Member = Depends(get_current_member)) -> StreamingResponse:
    await permissions.check(member, "stream_task", task_id)
    tasks = await reflect_table("tasks")

    async def events():
        async with engine.begin() as conn:
            row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
        if not row:
            yield f"data: {json.dumps({'type': 'task_failed', 'error': 'Task not found'})}\n\n"
            return
        yield f"data: {json.dumps({'type': 'catch_up', 'task': dict(row)}, default=str)}\n\n"

        pubsub = redis_client.pubsub()
        await pubsub.subscribe(activity_channel(task_id))
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30)
                if not message:
                    await asyncio.sleep(0)
                    continue
                payload = json.loads(message["data"])
                yield f"data: {json.dumps(payload, default=str)}\n\n"
                if payload.get("type") in {"task_complete", "task_failed"}:
                    break
        finally:
            await pubsub.unsubscribe(activity_channel(task_id))
            await pubsub.close()

    return StreamingResponse(events(), media_type="text/event-stream")
