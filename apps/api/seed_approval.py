"""Seed a pending approval (and its awaiting task) for the default org.

Used by the Approvals UI E2E (apps/web/e2e/approvals.spec.ts) to get a
deterministic approval into the inbox without depending on a live model.
Prints shell-eval-able IDs:  APPROVAL_ID=...  TASK_ID=...
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import update

from core.config import settings
from core.db import engine, reflect_table


async def main() -> None:
    task_id = str(uuid.uuid4())
    approval_id = str(uuid.uuid4())
    tasks = await reflect_table("tasks")
    approvals = await reflect_table("approvals")
    async with engine.begin() as conn:
        # Clean slate: retire any pre-existing pending approvals for this org so
        # the inbox contains exactly the one we seed (the UI's "Approve all"
        # decides a single batch, so leftovers would make the test ambiguous).
        await conn.execute(
            update(approvals)
            .where(approvals.c.organization_id == settings.org_id, approvals.c.status == "pending")
            .values(status="expired")
        )
        await conn.execute(
            tasks.insert().values(
                id=task_id,
                organization_id=settings.org_id,
                region=settings.region,
                triggered_by="manual",
                goal="E2E seeded approval task",
                status="awaiting_approval",
            )
        )
        await conn.execute(
            approvals.insert().values(
                id=approval_id,
                organization_id=settings.org_id,
                task_id=task_id,
                step_id="agent_loop",
                action_type="gmail.draft",
                action_payload={"subject": "E2E seeded approval", "to": "a@example.com"},
                status="pending",
            )
        )
    print(f"APPROVAL_ID={approval_id}")
    print(f"TASK_ID={task_id}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
