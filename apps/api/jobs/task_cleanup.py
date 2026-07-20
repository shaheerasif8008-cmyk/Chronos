"""Leader-only reaper for durable task cancellation cleanup requests."""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from runtime.cancellation import reap_pending_task_cleanups


scheduler = AsyncIOScheduler()


async def reap_task_cleanups() -> list[str]:
    return await reap_pending_task_cleanups(limit=100)


scheduler.add_job(
    reap_task_cleanups,
    "interval",
    seconds=30,
    id="task-cancellation-cleanup",
    max_instances=1,
    coalesce=True,
)
