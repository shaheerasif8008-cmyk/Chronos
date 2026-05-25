"""Named task checkpoints — Category 5 (State Management), Step 3.

Per-step working state already lives on the tasks row (``result`` + ``agent_state``),
so these are not a parallel persistence channel. A checkpoint is an explicit, named,
inspectable freeze of the execution context, created when a DAG plan step declares
``"checkpoint": "<name>"``. Callers pass an already-public snapshot (private ``__``
keys stripped) so this module needs no knowledge of executor internals.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import insert, select

from core.db import engine, reflect_table
from runtime.agent_loop import emit_activity


async def create_checkpoint(
    task: dict[str, Any],
    name: str,
    snapshot: dict[str, Any],
    step_index: int,
) -> str:
    """Persist a named snapshot of the task's execution context and emit an event."""
    table = await reflect_table("task_checkpoints")
    async with engine.begin() as conn:
        row = await conn.execute(
            insert(table)
            .values(
                organization_id=task["organization_id"],
                region=task.get("region", "us"),
                task_id=task["id"],
                checkpoint_name=str(name),
                context_snapshot=snapshot,
                step_index=int(step_index),
            )
            .returning(table.c.id)
        )
        checkpoint_id = str(row.scalar_one())
    await emit_activity(
        task["id"],
        {"type": "checkpoint", "name": str(name), "step_index": int(step_index), "checkpoint_id": checkpoint_id},
    )
    return checkpoint_id


async def list_checkpoints(task_id: str) -> list[dict[str, Any]]:
    """Return all checkpoints for a task, oldest first, for inspection or rollback."""
    table = await reflect_table("task_checkpoints")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(table).where(table.c.task_id == task_id).order_by(table.c.created_at)
            )
        ).mappings().all()
    return [dict(row) for row in rows]
