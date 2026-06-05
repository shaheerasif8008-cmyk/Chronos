"""Scheduled tasks — CRUD for proactive task triggers.

A scheduled task runs a goal on a cadence (interval seconds or a cron string).
The poller in jobs/scheduled_tasks.py picks up due rows and runs them as real
Tasks. This router lets a member create, list, toggle, delete, and manually
fire schedules.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, insert, select, update

from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from jobs.scheduled_tasks import compute_next_run, run_due_scheduled_tasks

router = APIRouter(prefix="/schedules", tags=["schedules"])


class ScheduleRequest(BaseModel):
    name: str | None = None
    goal: str
    schedule_kind: str = "interval"
    interval_seconds: int | None = None
    cron: str | None = None
    run_at: datetime | None = None
    time_of_day: str | None = None
    day_of_week: str | int | None = None
    day_of_month: int | None = None
    trigger_source: str | None = None
    trigger_event_type: str | None = None
    persona_id: str | None = None
    workspace_id: str | None = None
    enabled: bool = True
    status: str = "active"


class ScheduleUpdate(BaseModel):
    name: str | None = None
    goal: str | None = None
    schedule_kind: str | None = None
    interval_seconds: int | None = None
    cron: str | None = None
    run_at: datetime | None = None
    time_of_day: str | None = None
    day_of_week: str | int | None = None
    day_of_month: int | None = None
    trigger_source: str | None = None
    trigger_event_type: str | None = None
    enabled: bool | None = None
    status: str | None = None


def _validate(kind: str, interval_seconds: int | None, cron: str | None) -> None:
    if kind == "interval":
        if not interval_seconds or interval_seconds <= 0:
            raise HTTPException(status_code=422, detail="interval_seconds must be a positive integer")
    elif kind == "cron":
        if not cron:
            raise HTTPException(status_code=422, detail="cron expression is required for cron schedules")
    elif kind in {"one_time", "daily", "weekly", "monthly", "webhook", "connector_trigger"}:
        return
    else:
        raise HTTPException(status_code=422, detail="Unsupported schedule_kind")


@router.post("/")
async def create_schedule(req: ScheduleRequest, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "create_schedule", settings.org_id)
    _validate(req.schedule_kind, req.interval_seconds, req.cron)

    now = datetime.now(timezone.utc)
    next_run = compute_next_run(req.model_dump(), now)
    scheduled = await reflect_table("scheduled_tasks")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(scheduled)
            .values(
                organization_id=member.organization_id,
                region=settings.region,
                name=req.name,
                goal=req.goal,
                schedule_kind=req.schedule_kind,
                interval_seconds=req.interval_seconds,
                cron=req.cron,
                run_at=req.run_at,
                time_of_day=req.time_of_day,
                day_of_week=str(req.day_of_week) if req.day_of_week is not None else None,
                day_of_month=req.day_of_month,
                trigger_source=req.trigger_source,
                trigger_event_type=req.trigger_event_type,
                persona_id=req.persona_id,
                workspace_id=req.workspace_id,
                enabled=req.enabled,
                status=req.status,
                next_run_at=next_run,
                created_by=member.id,
            )
            .returning(scheduled.c.id)
        )
        schedule_id = str(result.scalar_one())
    await audit.log(
        "schedule_created",
        member.id,
        "schedules.create",
        organization_id=member.organization_id,
        resource_type="scheduled_tasks",
        resource_id=schedule_id,
        payload={"goal": req.goal, "kind": req.schedule_kind},
    )
    return {"schedule_id": schedule_id, "next_run_at": next_run.isoformat() if next_run else None}


@router.get("/")
async def list_schedules(member: Member = Depends(get_current_member)) -> list[dict]:
    scheduled = await reflect_table("scheduled_tasks")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(scheduled)
                .where(scheduled.c.organization_id == member.organization_id)
                .order_by(scheduled.c.created_at.desc())
            )
        ).mappings().all()
    return [_serialize(dict(r)) for r in rows]


@router.patch("/{schedule_id}")
async def update_schedule(
    schedule_id: str, req: ScheduleUpdate, member: Member = Depends(get_current_member)
) -> dict:
    await permissions.check(member, "update_schedule", settings.org_id)
    scheduled = await reflect_table("scheduled_tasks")
    row = await _require_schedule(member, schedule_id)

    values = {k: v for k, v in req.model_dump(exclude_unset=True).items()}
    if not values:
        return _serialize(row)
    # Recompute next_run_at if cadence fields change.
    merged = {**row, **values}
    if {"schedule_kind", "interval_seconds", "cron", "run_at", "time_of_day", "day_of_week", "day_of_month"} & values.keys():
        values["next_run_at"] = compute_next_run(merged, datetime.now(timezone.utc))
    async with engine.begin() as conn:
        await conn.execute(update(scheduled).where(scheduled.c.id == schedule_id).values(**values))
    await audit.log(
        "schedule_updated",
        member.id,
        "schedules.update",
        organization_id=member.organization_id,
        resource_type="scheduled_tasks",
        resource_id=schedule_id,
        payload={"fields": sorted(values.keys())},
    )
    return _serialize({**merged, **values})


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "delete_schedule", settings.org_id)
    await _require_schedule(member, schedule_id)
    scheduled = await reflect_table("scheduled_tasks")
    async with engine.begin() as conn:
        await conn.execute(delete(scheduled).where(scheduled.c.id == schedule_id))
    await audit.log(
        "schedule_deleted",
        member.id,
        "schedules.delete",
        organization_id=member.organization_id,
        resource_type="scheduled_tasks",
        resource_id=schedule_id,
    )
    return {"schedule_id": schedule_id, "deleted": True}


@router.post("/{schedule_id}/run")
async def run_schedule_now(schedule_id: str, member: Member = Depends(get_current_member)) -> dict:
    """Force a due-check that includes this schedule by clearing its next_run_at."""
    await permissions.check(member, "run_schedule", settings.org_id)
    row = await _require_schedule(member, schedule_id)
    scheduled = await reflect_table("scheduled_tasks")
    async with engine.begin() as conn:
        await conn.execute(
            update(scheduled).where(scheduled.c.id == schedule_id).values(next_run_at=None)
        )
    spawned = await run_due_scheduled_tasks()
    return {"schedule_id": schedule_id, "spawned_task_ids": spawned, "goal": row["goal"]}


@router.get("/{schedule_id}/runs")
async def list_schedule_runs(schedule_id: str, member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "list_schedule_runs", schedule_id)
    await _require_schedule(member, schedule_id)
    runs = await reflect_table("scheduled_task_runs")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(runs)
                .where(runs.c.schedule_id == schedule_id, runs.c.organization_id == member.organization_id)
                .order_by(runs.c.created_at.desc())
            )
        ).mappings().all()
    return [_serialize_run(dict(r)) for r in rows]


async def _require_schedule(member: Member, schedule_id: str) -> dict:
    scheduled = await reflect_table("scheduled_tasks")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(scheduled).where(
                    scheduled.c.id == schedule_id,
                    scheduled.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return dict(row)


def _serialize(row: dict) -> dict:
    def _iso(value):
        return value.isoformat() if isinstance(value, datetime) else value

    return {
        "id": str(row.get("id")),
        "name": row.get("name"),
        "goal": row.get("goal"),
        "schedule_kind": row.get("schedule_kind"),
        "interval_seconds": row.get("interval_seconds"),
        "cron": row.get("cron"),
        "run_at": _iso(row.get("run_at")),
        "time_of_day": row.get("time_of_day"),
        "day_of_week": row.get("day_of_week"),
        "day_of_month": row.get("day_of_month"),
        "trigger_source": row.get("trigger_source"),
        "trigger_event_type": row.get("trigger_event_type"),
        "enabled": row.get("enabled"),
        "status": row.get("status") or ("active" if row.get("enabled") else "paused"),
        "last_run_at": _iso(row.get("last_run_at")),
        "next_run_at": _iso(row.get("next_run_at")),
        "last_task_id": str(row["last_task_id"]) if row.get("last_task_id") else None,
    }


def _serialize_run(row: dict) -> dict:
    def _iso(value):
        return value.isoformat() if isinstance(value, datetime) else value

    return {
        "id": str(row.get("id")),
        "schedule_id": str(row.get("schedule_id")) if row.get("schedule_id") else None,
        "task_id": str(row.get("task_id")) if row.get("task_id") else None,
        "workflow_run_id": row.get("workflow_run_id"),
        "monitor_id": str(row.get("monitor_id")) if row.get("monitor_id") else None,
        "status": row.get("status"),
        "trigger_source": row.get("trigger_source"),
        "evidence": row.get("evidence") or {},
        "next_run_at": _iso(row.get("next_run_at")),
        "created_at": _iso(row.get("created_at")),
    }
