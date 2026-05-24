"""
Task Executor — compatibility shim over the native agent loop.

All external imports that other modules depend on are preserved here:
    TaskExecutor, activity_channel, emit_activity, get_task, update_task,
    insert_task, approvals_ready_for_drafting, AGENT_LOOP_APPROVAL_STEP_ID.

Heavy lifting has moved to runtime/agent_loop.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert, select, update

from core.db import engine, reflect_table
from runtime.agent_loop import (
    activity_channel,
    emit_activity,
    get_task,
    run_loop,
    resume_after_approval,
    save_task,
)

# ── Public name kept for imports in approvals.py, sub_agent.py, tests ─────────
AGENT_LOOP_APPROVAL_STEP_ID = "agent_loop"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── Legacy DB helpers (re-exported so existing call-sites don't break) ─────────

async def update_task(task_id: str, **values: Any) -> None:
    await save_task(task_id, **values)


async def insert_task(values: dict[str, Any]) -> str:
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        result = await conn.execute(insert(tasks).values(**values).returning(tasks.c.id))
        return str(result.scalar_one())


def approvals_ready_for_drafting(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return approval rows that are approved and have not yet had a draft created."""
    return [
        row
        for row in rows
        if row["status"] == "approved"
        and not (row.get("action_payload") or {}).get("draft_result")
    ]


# ── TaskExecutor ───────────────────────────────────────────────────────────────

class TaskExecutor:
    """Thin wrapper over the native agent loop.  Public API unchanged."""

    async def run(self, task_id: str) -> None:
        task = await get_task(task_id)
        if not task:
            raise RuntimeError(f"Task not found: {task_id}")
        if task["status"] in {"complete", "failed", "cancelled"}:
            return
        await run_loop(task)

    async def resume(self, task_id: str) -> None:
        """Resume a loop that was paused for approval."""
        await resume_after_approval(task_id)
