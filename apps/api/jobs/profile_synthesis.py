from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

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
    )
    entry_id = await replace_synthesized_memory_entry(org_id=org_id, content=profile, embedding=embedding)
    await audit.log(
        "memory_write",
        "chronos",
        "memory.synthesize_profile",
        resource_type="memory",
        resource_id=entry_id,
        payload={"source": "synthesized"},
    )
    return entry_id


async def synthesize_all_org_profiles() -> None:
    await synthesize_org_profile(settings.org_id)


scheduler.add_job(synthesize_all_org_profiles, "cron", hour=3)
