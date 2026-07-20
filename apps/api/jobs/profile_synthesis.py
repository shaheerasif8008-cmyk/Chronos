from __future__ import annotations
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import update

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from jobs.context_update import propose_context_update

scheduler = AsyncIOScheduler()


async def synthesize_org_profile(org_id: str = "default") -> str | None:
    """Compatibility entrypoint for the retired transcript-synthesis job.

    Older releases scanned every conversation in an organization and directly
    persisted model output as organization-wide memory. That crossed member and
    project privacy boundaries and skipped human review. The scheduled seam now
    delegates to the reviewed context-suggestion pipeline, which reads only
    deliberately shared explicit organization memory and creates a pending
    proposal rather than durable context.
    """
    return await propose_context_update(org_id)


async def synthesize_all_org_profiles() -> None:
    await synthesize_org_profile(settings.org_id)


async def decay_stale_memories(org_id: str = "default") -> int:
    """Apply a 10% importance decay to memory entries not updated in 30+ days.

    This creates natural forgetting for facts the org stops referencing, while
    entries that are retrieved and used stay relevant (importance_score is
    refreshed on use by the extraction job). Entries below 0.05 are soft-deleted.

    Returns the number of entries touched.
    """
    memory_entries = await reflect_table("memory_entries")
    stale_threshold = datetime.now(timezone.utc) - timedelta(days=30)

    async with engine.begin() as conn:
        # Decay entries above the floor that haven't been touched in 30 days.
        decay_result = await conn.execute(
            update(memory_entries)
            .where(
                memory_entries.c.organization_id == org_id,
                memory_entries.c.updated_at < stale_threshold,
                memory_entries.c.importance_score > 0.05,
                memory_entries.c.is_deleted.is_(False),
                memory_entries.c.source != "synthesized",  # never decay the profile
            )
            .values(importance_score=memory_entries.c.importance_score * 0.9)
            .returning(memory_entries.c.id)
        )
        decayed = len(decay_result.all())

        # Soft-delete entries that have decayed below the floor.
        await conn.execute(
            update(memory_entries)
            .where(
                memory_entries.c.organization_id == org_id,
                memory_entries.c.importance_score <= 0.05,
                memory_entries.c.is_deleted.is_(False),
                memory_entries.c.source != "synthesized",
            )
            .values(is_deleted=True)
        )

    if decayed:
        await audit.log(
            "memory_decay",
            "chronos",
            "memory.decay_stale",
            organization_id=org_id,
            payload={"decayed_count": decayed},
        )
    return decayed


async def decay_all_org_memories() -> None:
    await decay_stale_memories(settings.org_id)


scheduler.add_job(synthesize_all_org_profiles, "cron", hour=3)
scheduler.add_job(decay_all_org_memories, "cron", day_of_week="sun", hour=4)
