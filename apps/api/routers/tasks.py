from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import insert, select

from core import audit, notifications, permissions, task_assignments
from core.activity_events import list_task_events
from core.modes import normalize_mode
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from core.governance import enforce_task_admission
from core.redis import redis_client
from core.task_envelope import build_task_envelope
from core.task_access import task_access_role, visibility_clause, visible_task
from runtime import cancellation, task_runner
from runtime.executor import activity_channel

router = APIRouter(prefix="/tasks", tags=["tasks"])

logger = logging.getLogger(__name__)

_ORG_ADMIN_ROLES = {"admin", "owner"}


async def _require_task(member: Member, task_id: str) -> dict:
    """Return a task visible to its creator, assignee, or break-glass admin."""
    row = await visible_task(member, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return row


async def _require_project_membership(member: Member, project_id: str) -> None:
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(project_members.c.id).where(
                    project_members.c.organization_id == member.organization_id,
                    project_members.c.project_id == project_id,
                    project_members.c.member_id == member.id,
                )
            )
        ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")


class CreateTaskRequest(BaseModel):
    goal: str
    conversation_id: str | None = None
    persona_id: str | None = None
    workspace_id: str | None = None
    model: str | None = None
    mode: str | None = None
    reasoning_effort: str | None = None
    project_id: str | None = None


class TaskAssignmentRequest(BaseModel):
    member_id: str
    note: str | None = Field(default=None, max_length=2000)


class TaskInterventionRequest(BaseModel):
    reason: str | None = None


async def create_task_record(
    *,
    goal: str,
    member: Member,
    triggered_by: str,
    persona_id: str | None = None,
    workspace_id: str | None = None,
    model: str | None = None,
    mode: str | None = None,
    reasoning_effort: str | None = None,
    project_id: str | None = None,
    attachments_context: list[dict] | None = None,
    project_knowledge: str | None = None,
    original_message: str | None = None,
    router_decision: dict | None = None,
    conversation_context: list[dict] | None = None,
    task_id: str | None = None,
    agent_state_patch: dict | None = None,
) -> str:
    """Insert a task row. The native model action loop orchestrates by default.

    `model` is the chat-model id chosen in the UI; it is resolved to a concrete
    litellm model string and stored in agent_state so the loop honours the picker.
    """
    from core.llm import default_chat_model_id, normalize_reasoning_effort, resolve_agent_model

    await permissions.check(member, "create_task", workspace_id or "default")
    await enforce_task_admission(member.organization_id)
    if project_id is not None:
        await _require_project_membership(member, project_id)
    resolved_model = resolve_agent_model(model or default_chat_model_id())
    normalized_reasoning_effort = normalize_reasoning_effort(reasoning_effort)
    normalized_mode = normalize_mode(mode)
    tasks = await reflect_table("tasks")
    raw_user_message = original_message if original_message is not None else goal
    task_id = task_id or str(uuid.uuid4())
    envelope = build_task_envelope(
        task_id=task_id,
        raw_user_message=raw_user_message,
        ui_title=goal,
        router_decision=router_decision,
        conversation_context=conversation_context,
        attachments=attachments_context,
    )
    agent_state: dict = {
        "agent_history": [],
        "iteration_count": 0,
        "model": resolved_model,
        "reasoning_effort": normalized_reasoning_effort,
        "attachments": attachments_context or [],
        "project_knowledge": project_knowledge or "",
        "original_user_message": raw_user_message,
        "task_envelope": envelope.model_dump(),
    }
    if agent_state_patch:
        agent_state.update(agent_state_patch)

    async with engine.begin() as conn:
        insert_values: dict = dict(
            id=task_id,
            organization_id=member.organization_id,
            region=settings.region,
            persona_id=persona_id,
            workspace_id=workspace_id,
            triggered_by=triggered_by,
            triggered_by_member_id=member.id,
            status="queued",
            goal=goal,
            plan={},
            agent_state=agent_state,
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
        new_task_id = str(result.scalar_one())

    await permissions.grant_task_role(
        str(member.id), "owner", new_task_id, member.organization_id
    )
    return new_task_id


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
        reasoning_effort=req.reasoning_effort,
        project_id=req.project_id,
        original_message=req.goal,
        router_decision={"mode": "agent", "ui_title": req.goal},
    )
    await task_runner.enqueue_task(task_id)
    return {"task_id": task_id, "status": "queued"}


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, member: Member = Depends(get_current_member)) -> dict:
    row = await _require_task(member, task_id)
    await permissions.check(member, "cancel_task", task_id)
    if row["status"] in {"complete", "failed"}:
        return {
            "task_id": task_id,
            "status": row["status"],
            "cancelled": False,
            "cleanup": await cancellation.get_task_cleanup(
                organization_id=member.organization_id, task_id=task_id
            ),
        }
    try:
        return await cancellation.request_task_cancellation(
            organization_id=member.organization_id,
            task_id=task_id,
            actor_id=member.id,
            reason="user_cancelled",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


@router.post("/{task_id}/retry")
async def retry_task(task_id: str, member: Member = Depends(get_current_member)) -> dict:
    """Revive a failed / dead-lettered task: clear terminal state and re-enqueue."""
    row = await _require_task(member, task_id)
    await permissions.check(member, "retry_task", task_id)
    if row["status"] not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail=f"Task is {row['status']}, not retryable")
    await task_runner.requeue_task(task_id)
    return {"task_id": task_id, "status": "queued", "retried": True}


@router.post("/{task_id}/pause")
async def pause_task(
    task_id: str,
    req: TaskInterventionRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    row = await _require_task(member, task_id)
    await permissions.check(member, "pause_task", task_id)
    if row["status"] == "paused":
        return {"task_id": task_id, "status": "paused", "paused": False}
    if row["status"] in {"complete", "failed", "cancelled", "awaiting_approval"}:
        raise HTTPException(status_code=409, detail=f"Task is {row['status']}, not pausable")
    reason = (req.reason or "operator pause").strip()[:500] or "operator pause"
    paused = await task_runner.pause_task(task_id, reason=reason)
    if not paused:
        raise HTTPException(status_code=409, detail="Task state changed before it could be paused")
    return {"task_id": task_id, "status": "paused", "paused": True, "reason": reason}


@router.post("/{task_id}/resume")
async def resume_task(task_id: str, member: Member = Depends(get_current_member)) -> dict:
    row = await _require_task(member, task_id)
    await permissions.check(member, "resume_task", task_id)
    if row["status"] != "paused":
        raise HTTPException(status_code=409, detail=f"Task is {row['status']}, not paused")
    resumed = await task_runner.resume_task(task_id)
    if not resumed:
        raise HTTPException(status_code=409, detail="Task was already resumed by another request")
    return {"task_id": task_id, "status": "queued", "resumed": True}


@router.get("/")
async def list_tasks(
    status: str | None = Query(default=None),
    dead_letter: bool | None = Query(default=None),
    include_children: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await permissions.check(member, "list_tasks", settings.org_id)
    tasks = await reflect_table("tasks")
    stmt = select(tasks).where(tasks.c.organization_id == member.organization_id)
    if member.role not in _ORG_ADMIN_ROLES:
        stmt = stmt.where(visibility_clause(tasks, member))
    if not include_children:
        stmt = stmt.where(tasks.c.parent_task_id.is_(None))
    if status:
        stmt = stmt.where(tasks.c.status == status)
    if dead_letter is not None:
        stmt = stmt.where(tasks.c.dead_letter == dead_letter)
    stmt = stmt.order_by(tasks.c.created_at.desc()).limit(limit).offset(offset)
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(row) for row in rows]


@router.get("/{task_id}/cleanup")
async def get_task_cleanup_status(
    task_id: str, member: Member = Depends(get_current_member)
) -> dict:
    await _require_task(member, task_id)
    await permissions.check(member, "cancel_task", task_id)
    cleanup = await cancellation.get_task_cleanup(
        organization_id=member.organization_id, task_id=task_id
    )
    if cleanup is None:
        raise HTTPException(status_code=404, detail="Task cleanup not found")
    return cleanup


@router.get("/{task_id}")
async def get_task_detail(task_id: str, member: Member = Depends(get_current_member)) -> dict:
    row = await _require_task(member, task_id)
    await permissions.check(member, "view_task", task_id)
    return row


def _can_manage_assignment(member: Member, task: dict, *, allow_assignee: bool) -> bool:
    role = task_access_role(member, task)
    return role in ({"admin", "owner", "assignee"} if allow_assignee else {"admin", "owner"})


async def _notify_assignment(
    *,
    member: Member,
    task: dict,
    target_member: dict | None,
    event_type: str,
    note: str | None,
) -> None:
    if not target_member or str(target_member["id"]) == str(member.id):
        return
    verb = "handed off" if event_type == "handoff" else "assigned"
    try:
        await notifications.emit(
            organization_id=member.organization_id,
            type="task_assignment",
            title=f"{member.name or member.email} {verb} a task to you",
            body=note or str(task.get("goal") or "")[:240],
            severity="info",
            member_id=str(target_member["id"]),
            resource_type="task",
            resource_id=str(task["id"]),
            created_by=member.id,
        )
    except Exception:  # noqa: BLE001 - assignment is durable even if delivery is degraded
        logger.warning(
            "task assignment notification failed for member %s",
            target_member.get("id"),
            exc_info=True,
        )


async def _sync_assignment_tuple(
    *,
    task_id: str,
    organization_id: str,
    old_member_id: str | None,
    new_member_id: str | None,
) -> None:
    # Grant before revoking so an OpenFGA sync cannot create an avoidable gap.
    # The SQL task row remains canonical and still denies a stale extra tuple.
    if new_member_id:
        await permissions.grant_task_role(
            new_member_id, "editor", task_id, organization_id
        )
    if old_member_id and old_member_id != new_member_id:
        await permissions.revoke_task_role(old_member_id, "editor", task_id)


async def _change_task_assignment(
    *,
    task: dict,
    req: TaskAssignmentRequest | None,
    member: Member,
    event_type: str,
) -> dict:
    target_member_id = req.member_id if req else None
    note = req.note.strip() if req and req.note else None
    if target_member_id and str(target_member_id) == str(task.get("triggered_by_member_id")):
        raise HTTPException(
            status_code=422,
            detail="The task creator already owns the task; unassign it instead",
        )
    old_member_id = str(task.get("assignee_member_id")) if task.get("assignee_member_id") else None
    try:
        updated, event, target = await task_assignments.change_assignment(
            organization_id=member.organization_id,
            task_id=str(task["id"]),
            actor_member_id=member.id,
            to_member_id=target_member_id,
            event_type=event_type,
            note=note,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Task or member not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    new_member_id = (
        str(updated.get("assignee_member_id"))
        if updated.get("assignee_member_id")
        else None
    )
    await _sync_assignment_tuple(
        task_id=str(task["id"]),
        organization_id=member.organization_id,
        old_member_id=old_member_id,
        new_member_id=new_member_id,
    )
    await audit.log(
        "task_assignment",
        member.id,
        f"tasks.{event['event_type']}",
        organization_id=member.organization_id,
        resource_type="tasks",
        resource_id=str(task["id"]),
        payload={
            "event_id": str(event["id"]),
            "from_member_id": event.get("from_member_id"),
            "to_member_id": event.get("to_member_id"),
            "note": event.get("note"),
        },
    )
    await _notify_assignment(
        member=member,
        task=updated,
        target_member=target,
        event_type=str(event["event_type"]),
        note=note,
    )
    return {"task": updated, "assignment_event": event, "assignee": target}


@router.get("/{task_id}/assignment")
async def get_task_assignment(
    task_id: str,
    member: Member = Depends(get_current_member),
) -> dict:
    task = await _require_task(member, task_id)
    await permissions.check(member, "view_task_assignment", task_id)
    assignee = None
    if task.get("assignee_member_id"):
        assignee = await task_assignments.active_org_member(
            member.organization_id, str(task["assignee_member_id"])
        )
    return {
        "task_id": task_id,
        "assignee": assignee,
        "assigned_by_member_id": task.get("assigned_by_member_id"),
        "assigned_at": task.get("assigned_at"),
    }


@router.get("/{task_id}/assignment/history")
async def get_task_assignment_history(
    task_id: str,
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await _require_task(member, task_id)
    await permissions.check(member, "view_task_assignment", task_id)
    return await task_assignments.assignment_history(member.organization_id, task_id)


@router.put("/{task_id}/assignment")
async def assign_task(
    task_id: str,
    req: TaskAssignmentRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    task = await _require_task(member, task_id)
    if not _can_manage_assignment(member, task, allow_assignee=False):
        raise HTTPException(status_code=403, detail="Only the task owner can assign it")
    await permissions.check(member, "assign_task", task_id)
    return await _change_task_assignment(
        task=task, req=req, member=member, event_type="assigned"
    )


@router.post("/{task_id}/handoff")
async def handoff_task(
    task_id: str,
    req: TaskAssignmentRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    task = await _require_task(member, task_id)
    if not _can_manage_assignment(member, task, allow_assignee=True):
        raise HTTPException(status_code=403, detail="Task handoff is not allowed")
    await permissions.check(member, "handoff_task", task_id)
    return await _change_task_assignment(
        task=task, req=req, member=member, event_type="handoff"
    )


@router.delete("/{task_id}/assignment")
async def unassign_task(
    task_id: str,
    member: Member = Depends(get_current_member),
) -> dict:
    task = await _require_task(member, task_id)
    if not _can_manage_assignment(member, task, allow_assignee=False):
        raise HTTPException(status_code=403, detail="Only the task owner can unassign it")
    await permissions.check(member, "unassign_task", task_id)
    return await _change_task_assignment(
        task=task, req=None, member=member, event_type="unassigned"
    )


@router.get("/{task_id}/events")
async def get_task_events(
    task_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await _require_task(member, task_id)
    await permissions.check(member, "view_task_events", task_id)
    return await list_task_events(task_id, member.organization_id, limit=limit, offset=offset)


@router.get("/{task_id}/stream")
async def stream_task(task_id: str, member: Member = Depends(get_current_member)) -> StreamingResponse:
    initial_task = await _require_task(member, task_id)
    await permissions.check(member, "stream_task", task_id)

    async def events():
        replay_events = await list_task_events(task_id, member.organization_id, limit=200, offset=0)
        yield f"data: {json.dumps({'type': 'catch_up', 'task': initial_task, 'events': replay_events}, default=str)}\n\n"

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
