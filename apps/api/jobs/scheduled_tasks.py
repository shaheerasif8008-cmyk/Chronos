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
from calendar import monthrange
from datetime import datetime, time, timedelta, timezone
from typing import Any

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


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_time_of_day(value: Any) -> time | None:
    if isinstance(value, time):
        return value
    if not value:
        return None
    try:
        hour, minute, *rest = str(value).split(":")
        second = int(rest[0]) if rest else 0
        return time(int(hour), int(minute), second, tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _weekday(value: Any) -> int | None:
    if isinstance(value, int) and 0 <= value <= 6:
        return value
    names = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    return names.get(str(value or "").strip().lower())


def compute_next_run(row: dict, after: datetime) -> datetime | None:
    """Next fire time strictly after ``after`` for a scheduled_tasks row."""
    kind = row.get("schedule_kind") or "interval"
    if kind == "one_time":
        run_at = _parse_datetime(row.get("run_at"))
        return run_at if run_at and run_at > after else None
    if kind == "daily":
        tod = _parse_time_of_day(row.get("time_of_day"))
        if tod is None:
            return None
        candidate = after.replace(hour=tod.hour, minute=tod.minute, second=tod.second, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate
    if kind == "weekly":
        tod = _parse_time_of_day(row.get("time_of_day"))
        weekday = _weekday(row.get("day_of_week"))
        if tod is None or weekday is None:
            return None
        days = (weekday - after.weekday()) % 7
        candidate = (after + timedelta(days=days)).replace(hour=tod.hour, minute=tod.minute, second=tod.second, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate
    if kind == "monthly":
        tod = _parse_time_of_day(row.get("time_of_day"))
        if tod is None:
            return None
        try:
            desired_day = max(1, int(row.get("day_of_month") or 1))
        except (TypeError, ValueError):
            return None
        year, month = after.year, after.month
        for _ in range(14):
            day = min(desired_day, monthrange(year, month)[1])
            candidate = after.replace(year=year, month=month, day=day, hour=tod.hour, minute=tod.minute, second=tod.second, microsecond=0)
            if candidate > after:
                return candidate
            month += 1
            if month == 13:
                year += 1
                month = 1
        return None
    if kind in {"webhook", "connector_trigger"}:
        return None
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


async def _record_schedule_run(
    *,
    schedule_id: str,
    organization_id: str,
    status: str,
    trigger_source: str,
    task_id: str | None = None,
    workflow_run_id: str | None = None,
    monitor_id: str | None = None,
    evidence: dict[str, Any] | None = None,
    next_run_at: datetime | None = None,
) -> str | None:
    from sqlalchemy import insert

    try:
        runs = await reflect_table("scheduled_task_runs")
    except Exception:
        return None
    try:
        stmt = (
            insert(runs)
            .values(
                organization_id=organization_id,
                region=settings.region,
                schedule_id=schedule_id,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                monitor_id=monitor_id,
                status=status,
                trigger_source=trigger_source,
                evidence=evidence or {},
                next_run_at=next_run_at,
            )
            .returning(runs.c.id)
        )
    except Exception:
        return None
    async with engine.begin() as conn:
        result = await conn.execute(stmt)
        return str(result.scalar_one())


def evaluate_monitor_result(monitor: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any] | None:
    condition = monitor.get("condition") or {}
    operator = condition.get("operator") or "changed"
    previous = monitor.get("last_evidence") or {}
    changed = observed.get("hash") and observed.get("hash") != previous.get("hash")
    matched = False
    if operator == "changed":
        matched = bool(changed)
    elif operator == "contains":
        needle = str(condition.get("value") or "").lower()
        haystack = f"{observed.get('title', '')} {observed.get('snippet', '')}".lower()
        matched = bool(needle and needle in haystack)
    elif operator == "always":
        matched = True
    if not matched:
        return None
    summary = observed.get("snippet") or f"{monitor.get('name') or monitor.get('target')} matched monitor condition"
    return {
        "monitor_id": str(monitor["id"]),
        "organization_id": monitor["organization_id"],
        "severity": condition.get("severity") or "info",
        "summary": summary,
        "evidence": {
            "url": observed.get("url") or monitor.get("target"),
            "title": observed.get("title"),
            "snippet": observed.get("snippet"),
            "hash": observed.get("hash"),
            "observed_at": observed.get("observed_at") or _now().isoformat(),
        },
    }


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
        if row.get("status") == "paused":
            continue
        task_id = await _spawn_task(row)
        spawned.append(task_id)
        next_run = compute_next_run(row, now)
        async with engine.begin() as conn:
            await conn.execute(
                update(scheduled)
                .where(scheduled.c.id == row["id"])
                .values(last_run_at=now, next_run_at=next_run, last_task_id=task_id)
            )
        await _record_schedule_run(
            schedule_id=str(row["id"]),
            organization_id=row["organization_id"],
            status="triggered",
            trigger_source="scheduler",
            task_id=task_id,
            next_run_at=next_run,
        )
        await audit.log(
            "scheduled_task_triggered",
            "scheduler",
            "scheduled_tasks.run",
            organization_id=row["organization_id"],
            resource_type="scheduled_tasks",
            resource_id=str(row["id"]),
            payload={"task_id": task_id, "next_run_at": next_run.isoformat() if next_run else None},
        )
    return spawned


scheduler.add_job(run_due_scheduled_tasks, "interval", seconds=_POLL_SECONDS)
