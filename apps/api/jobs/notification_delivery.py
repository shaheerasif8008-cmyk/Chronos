"""Leader-only automatic notification email and weekly-digest schedules."""

from datetime import timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.notification_delivery import run_delivery_cycle, run_weekly_digest_cycle

scheduler = AsyncIOScheduler()

# Retry/new-notification dispatcher.  ``main`` starts this scheduler paused and
# only the Redis-elected leader resumes it, while receipt claims remain safe if
# an operator invokes delivery concurrently.
scheduler.add_job(
    run_delivery_cycle,
    "interval",
    seconds=60,
    id="notification-email-delivery",
    coalesce=True,
    max_instances=1,
    misfire_grace_time=120,
)

# Monday 09:00 UTC closes the previous ISO week. Stable per-member/week keys in
# the receipt table make misfire/coalescing and operator re-runs idempotent.
scheduler.add_job(
    run_weekly_digest_cycle,
    "cron",
    day_of_week="mon",
    hour=9,
    minute=0,
    timezone=timezone.utc,
    id="notification-weekly-digest",
    coalesce=True,
    max_instances=1,
    misfire_grace_time=6 * 60 * 60,
)
