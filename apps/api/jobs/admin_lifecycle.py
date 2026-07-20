"""Leader-only execution of confirmed workspace deletion tombstones."""

from datetime import timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.admin_lifecycle import process_due_workspace_deletions


scheduler = AsyncIOScheduler()
scheduler.add_job(
    process_due_workspace_deletions,
    "cron",
    hour=5,
    minute=30,
    timezone=timezone.utc,
    id="workspace-deletion-lifecycle",
    coalesce=True,
    max_instances=1,
    misfire_grace_time=3600,
)
