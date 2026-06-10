from __future__ import annotations
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.llm import complete_text
from core.memory_writes import embedding_literal_for_memory, replace_synthesized_memory_entry

scheduler = AsyncIOScheduler()


async def synthesize_org_profile(org_id: str = "default") -> str | None:
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(messages.c.role, messages.c.content, messages.c.created_at)
                .select_from(messages.join(conversations, messages.c.conversation_id == conversations.c.id))
                .where(
                    messages.c.organization_id == org_id,
                    messages.c.created_at >= since,
                )
                .order_by(messages.c.created_at.asc())
                .limit(200)
            )
        ).mappings().all()
    if not rows:
        return None

    transcript = "\n".join(f"{row['role']}: {row['content']}" for row in rows)
    profile = (
        await complete_text(
            "Based on these recent conversations, summarize communication patterns, "
            "recurring topics, key people, and domain vocabulary. Keep it under 500 words.\n\n"
            f"{transcript}"
        )
    ).strip()
    if not profile:
        return None

    embedding = await embedding_literal_for_memory(
        profile,
        actor_id="chronos",
        action="memory.synthesize_profile",
        organization_id=org_id,
    )
    entry_id = await replace_synthesized_memory_entry(org_id=org_id, content=profile, embedding=embedding)
    await audit.log(
        "memory_write",
        "chronos",
        "memory.synthesize_profile",
        organization_id=org_id,
        resource_type="memory",
        resource_id=entry_id,
        payload={"source": "synthesized"},
    )
    return entry_id


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
