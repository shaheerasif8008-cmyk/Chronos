"""Leader-only cleanup for expired cloud-computer consent windows."""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from connectors.computer import computer_connector


scheduler = AsyncIOScheduler()


async def cleanup_expired_computer_sessions() -> int:
    return await computer_connector.cleanup_expired_sessions(limit=200)


# ``main`` starts this scheduler paused. Only the Redis-elected leader resumes
# it, so multiple API replicas cannot race provider destruction and audit rows.
scheduler.add_job(
    cleanup_expired_computer_sessions,
    "interval",
    seconds=60,
    id="computer-session-expiry",
    max_instances=1,
    coalesce=True,
)
