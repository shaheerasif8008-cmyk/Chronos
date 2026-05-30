"""Proactive task triggers.

A scheduled_tasks row says "run this goal on this cadence". A lightweight
poll (every 30s) finds due rows, materializes a real Task for each, runs it
through the normal executor (so planning, the broker seam, approvals, and
activity streaming all apply unchanged), and computes the next fire time.

Cadence is computed with APScheduler's own triggers (already a dependency):
IntervalTrigger for ``interval`` schedules, CronTrigger for ``cron`` strings.
The proactive trigger is just a Task whose ``triggered_by`` is ``"schedule"`` —
nothing downstream needs to know it wasn't started by a human.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, update

from core import audit
from core.config import settings
from core.db import engine, reflect_table

scheduler = AsyncIOScheduler()

_POLL_SECONDS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def compute_next_run(row: dict, after: datetime) -> datetime | None:
    """Next fire time strictly after ``after`` for a scheduled_tasks row."""
    kind = row.get("schedule_kind") or "interval"
    if kind == "cron" and row.get("cron"):
        try:
            trigger = CronTrigger.from_crontab(str(row["cron"]), timezone=timezone.utc)
        except (ValueError, TypeError):
            return None
        return trigger.get_next_fire_time(None, after)
    seconds = row.get("interval_seconds")
    if not seconds or int(seconds) <= 0:
        return None
    return after + timedelta(seconds=int(seconds))


async def _spawn_task(row: dict) -> str:
    """Materialize and start a real Task for a due schedule. Returns the task id."""
    # Imported lazily to avoid a circular import at module load (executor → jobs).
    from runtime.executor import TaskExecutor, insert_task

    task_id = await insert_task(
        {
            "organization_id": row["organization_id"],
            "region": row.get("region", settings.region),
            "goal": row["goal"],
            "status": "pending",
            "triggered_by": "schedule",
            "triggered_by_member_id": row.get("created_by"),
            "persona_id": row.get("persona_id"),
            "workspace_id": row.get("workspace_id"),
        }
    )
    asyncio.create_task(TaskExecutor().run(task_id))
    return task_id


async def run_due_scheduled_tasks(now: datetime | None = None) -> list[str]:
    """Find enabled, due schedules; spawn a task for each; advance next_run_at.

    A row is due when enabled and ``next_run_at`` is null (never run) or <= now.
    Returns the list of spawned task ids.
    """
    now = now or _now()
    scheduled = await reflect_table("scheduled_tasks")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(scheduled).where(
                    scheduled.c.enabled.is_(True),
                    (scheduled.c.next_run_at.is_(None)) | (scheduled.c.next_run_at <= now),
                )
            )
        ).mappings().all()
    rows = [dict(r) for r in rows]

    spawned: list[str] = []
    for row in rows:
        task_id = await _spawn_task(row)
        spawned.append(task_id)
        next_run = compute_next_run(row, now)
        async with engine.begin() as conn:
            await conn.execute(
                update(scheduled)
                .where(scheduled.c.id == row["id"])
                .values(last_run_at=now, next_run_at=next_run, last_task_id=task_id)
            )
        await audit.log(
            "scheduled_task_triggered",
            "scheduler",
            "scheduled_tasks.run",
            resource_type="scheduled_tasks",
            resource_id=str(row["id"]),
            payload={"task_id": task_id, "next_run_at": next_run.isoformat() if next_run else None},
        )
    return spawned


scheduler.add_job(run_due_scheduled_tasks, "interval", seconds=_POLL_SECONDS)
