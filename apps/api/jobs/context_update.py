from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import insert, select

from core import audit
from core.config import settings
from core.context import ROOT
from core.db import engine, reflect_table
from core.llm import complete_text

scheduler = AsyncIOScheduler()


async def propose_context_update(org_id: str = "default") -> str | None:
    org_path = Path(ROOT) / "context" / org_id / "org.md"
    current_context = org_path.read_text() if org_path.exists() else ""
    messages = await reflect_table("messages")
    since = datetime.now(timezone.utc) - timedelta(days=1)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(messages.c.role, messages.c.content)
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
    suggestion = (
        await complete_text(
            "Identify meaningful new organization facts from recent conversations "
            "that are missing from org.md. Return either an empty string or a concise "
            "proposed Markdown patch. Do not rewrite the whole file.\n\n"
            f"org.md:\n{current_context}\n\nRecent conversations:\n{transcript}"
        )
    ).strip()
    if not suggestion:
        return None

    context_suggestions = await reflect_table("context_suggestions")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(context_suggestions)
            .values(
                organization_id=org_id,
                region=settings.region,
                status="pending",
                suggested_patch=suggestion,
                source="context_update_job",
            )
            .returning(context_suggestions.c.id)
        )
        suggestion_id = str(result.scalar_one())
    await audit.log(
        "context_update_suggested",
        "chronos",
        "context.propose_update",
        organization_id=org_id,
        resource_type="context_suggestions",
        resource_id=suggestion_id,
    )
    return suggestion_id


async def propose_all_context_updates() -> None:
    await propose_context_update(settings.org_id)


scheduler.add_job(propose_all_context_updates, "interval", hours=24)
