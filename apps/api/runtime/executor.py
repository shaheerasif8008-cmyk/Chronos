"""
Task Executor — thin shim over the native agent loop.

Preserves the public names other modules import (TaskExecutor, activity_channel,
emit_activity, get_task, update_task, insert_task, approvals_ready_for_drafting,
AGENT_LOOP_APPROVAL_STEP_ID). The DAG planner/executor has been removed; every task
runs the native loop in runtime/agent_loop.py.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert

from core.db import engine, reflect_table
from runtime.agent_loop import (
    activity_channel,
    emit_activity,
    get_task,
    run_loop,
    resume_after_approval,
    save_task,
)

log = logging.getLogger(__name__)

# Public surface — several names are re-exported from agent_loop for modules that
# import them from here (e.g. routers/tasks.py imports activity_channel).
__all__ = [
    "TaskExecutor",
    "activity_channel",
    "emit_activity",
    "get_task",
    "update_task",
    "insert_task",
    "approvals_ready_for_drafting",
    "AGENT_LOOP_APPROVAL_STEP_ID",
    "now_utc",
    "run_loop",
    "resume_after_approval",
    "save_task",
]

AGENT_LOOP_APPROVAL_STEP_ID = "agent_loop"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


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


class TaskExecutor:
    """Run a task through the native agent loop (background/durable tier)."""

    async def run(self, task_id: str) -> None:
        task = await get_task(task_id)
        if not task:
            raise RuntimeError(f"Task not found: {task_id}")
        if task["status"] in {"complete", "failed", "cancelled"}:
            return
        try:
            await save_task(task_id, status="running", started_at=now_utc())
            await run_loop(task)
        except asyncio.CancelledError:
            await save_task(
                task_id, status="cancelled",
                error="Task execution was cancelled.", completed_at=now_utc(),
            )
            raise
        except Exception as exc:
            error = f"executor_error: {type(exc).__name__}: {exc}"
            log.exception("Task %s crashed in executor", task_id)
            await save_task(task_id, status="failed", error=error, completed_at=now_utc())
            await emit_activity(task_id, {"type": "task_failed", "error": error})
            raise

    async def resume(self, task_id: str) -> None:
        """Resume after an approval pause or a process crash."""
        task = await get_task(task_id)
        if not task:
            return
        if task["status"] in {"complete", "failed", "cancelled"}:
            return
        if task["status"] in {"awaiting_approval", "paused"}:
            await resume_after_approval(task_id)
            return
        state = task.get("agent_state") or {}
        if isinstance(state, dict) and state.get("agent_history"):
            await run_loop(task)
        else:
            await self.run(task_id)
