"""Durable task assignment and handoff state.

``tasks.assignee_member_id`` is the current responsibility pointer used by the
authorization policy.  ``task_assignment_events`` is an append-only history of
every transition, including who initiated it and an optional handoff note.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert, select, update

from core.config import settings
from core.db import engine, reflect_table


ASSIGNMENT_EVENT_TYPES = {"assigned", "reassigned", "handoff", "unassigned"}


async def active_org_member(organization_id: str, member_id: str) -> dict[str, Any] | None:
    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(
                    members.c.id,
                    members.c.email,
                    members.c.name,
                    members.c.role,
                    members.c.status,
                ).where(
                    members.c.id == str(member_id),
                    members.c.organization_id == organization_id,
                    members.c.status == "active",
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def assignment_history(
    organization_id: str,
    task_id: str,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    events = await reflect_table("task_assignment_events")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(events)
                .where(
                    events.c.organization_id == organization_id,
                    events.c.task_id == str(task_id),
                )
                .order_by(events.c.created_at.asc(), events.c.id.asc())
                .limit(limit)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def change_assignment(
    *,
    organization_id: str,
    task_id: str,
    actor_member_id: str,
    to_member_id: str | None,
    event_type: str,
    note: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Atomically update current assignment and append its history event.

    Returns ``(updated_task, event, target_member)``.  The target is validated
    as an active member of the same organization before the task row is locked.
    """

    if event_type not in ASSIGNMENT_EVENT_TYPES:
        raise ValueError("Invalid assignment event type")
    if event_type == "unassigned" and to_member_id is not None:
        raise ValueError("Unassignment cannot include a target member")
    if event_type != "unassigned" and not to_member_id:
        raise ValueError("Assignment requires a target member")

    target = None
    if to_member_id is not None:
        target = await active_org_member(organization_id, str(to_member_id))
        if target is None:
            raise LookupError("Member not found")

    tasks = await reflect_table("tasks")
    events = await reflect_table("task_assignment_events")
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        current = (
            await conn.execute(
                select(tasks)
                .where(
                    tasks.c.id == str(task_id),
                    tasks.c.organization_id == organization_id,
                )
                .with_for_update()
            )
        ).mappings().first()
        if current is None:
            raise LookupError("Task not found")
        current = dict(current)
        from_member_id = current.get("assignee_member_id")
        if event_type == "handoff" and not from_member_id:
            raise ValueError("Task must be assigned before it can be handed off")
        if event_type == "unassigned" and not from_member_id:
            raise ValueError("Task is not assigned")
        if to_member_id is not None and str(from_member_id or "") == str(to_member_id):
            raise ValueError("Task is already assigned to that member")

        effective_type = event_type
        if event_type == "assigned" and from_member_id:
            effective_type = "reassigned"

        updated = (
            await conn.execute(
                update(tasks)
                .where(
                    tasks.c.id == str(task_id),
                    tasks.c.organization_id == organization_id,
                )
                .values(
                    assignee_member_id=str(to_member_id) if to_member_id else None,
                    assigned_by_member_id=str(actor_member_id) if to_member_id else None,
                    assigned_at=now if to_member_id else None,
                )
                .returning(tasks)
            )
        ).mappings().first()
        event = (
            await conn.execute(
                insert(events)
                .values(
                    organization_id=organization_id,
                    region=settings.region,
                    task_id=str(task_id),
                    from_member_id=str(from_member_id) if from_member_id else None,
                    to_member_id=str(to_member_id) if to_member_id else None,
                    actor_member_id=str(actor_member_id),
                    event_type=effective_type,
                    note=note,
                )
                .returning(events)
            )
        ).mappings().first()
    return dict(updated), dict(event), target
