"""Leader-only schedule for application data retention."""

from datetime import timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.retention import run_all_org_retention

scheduler = AsyncIOScheduler()

# Run after the context/memory maintenance windows.  ``main`` starts every
# scheduler paused and only the Redis-elected leader resumes it, so this job is
# single-firing even when the API has multiple replicas.
scheduler.add_job(
    run_all_org_retention,
    "cron",
    hour=5,
    minute=15,
    timezone=timezone.utc,
    id="application-data-retention",
    coalesce=True,
    max_instances=1,
    misfire_grace_time=3600,
)
