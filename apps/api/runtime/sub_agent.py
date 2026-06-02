from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import select, update

from core import audit, permissions
from core.config import settings
from core.db import engine, reflect_table
from core.exceptions import ChronosError
from core.redis import redis_client
from runtime.executor import emit_activity, get_task, insert_task


class DepthLimitExceeded(ChronosError):
    """Raised before INSERT when a sub-agent spawn would exceed the recursion limit."""


class SubAgentFailed(ChronosError):
    """Raised when a child task fails."""


# Module-level semaphore: limits concurrent sub-agents across the whole process.
# Real multi-org enforcement would need a Redis-backed counter; this is sufficient
# for single-process Phase 1.
_spawn_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _spawn_semaphore
    if _spawn_semaphore is None or _spawn_semaphore._value != settings.concurrent_sub_agents:  # type: ignore[attr-defined]
        _spawn_semaphore = asyncio.Semaphore(settings.concurrent_sub_agents)
    return _spawn_semaphore


class SubAgentManager:
    async def spawn_and_wait(self, parent_task: dict[str, Any], goal: str) -> dict[str, Any]:
        """Spawn a single child task and block until it completes."""
        return await self._spawn_one(parent_task, goal)

    async def spawn_many(self, parent_task: dict[str, Any], goals: list[str]) -> list[dict[str, Any]]:
        """Spawn N child tasks in parallel (bounded by concurrent_sub_agents semaphore)."""
        async def one(goal: str) -> dict[str, Any]:
            async with _get_semaphore():
                return await self._spawn_one(parent_task, goal)

        return list(await asyncio.gather(*[one(g) for g in goals], return_exceptions=False))

    async def _spawn_one(self, parent_task: dict[str, Any], goal: str) -> dict[str, Any]:
        if int(parent_task.get("depth") or 0) >= 3:
            allowed = bool(parent_task.get("allow_deep_spawn")) and await permissions.check(
                _actor(parent_task),
                "spawn_sub_agent_beyond_depth_3",
                parent_task.get("workspace_id") or "default",
            )
            if not allowed:
                await audit.log(
                    "sub_agent_depth_blocked",
                    parent_task.get("triggered_by_member_id") or "chronos",
                    "sub_agent.spawn",
                    organization_id=parent_task["organization_id"],
                    resource_type="tasks",
                    resource_id=parent_task["id"],
                    decision="denied",
                )
                raise DepthLimitExceeded("Max sub-agent depth (3) reached")

        # Context isolation: child starts with ONLY its goal and no parent history.
        # The model-in-loop executor will seed a fresh history from the goal alone.
        sub_task_id = await insert_task(
            {
                "organization_id": parent_task["organization_id"],
                "region": parent_task["region"],
                "parent_task_id": parent_task["id"],
                "persona_id": parent_task.get("persona_id"),
                "workspace_id": parent_task.get("workspace_id"),
                "triggered_by": f"task:{parent_task['id']}",
                "triggered_by_member_id": parent_task.get("triggered_by_member_id"),
                "status": "pending",
                "goal": goal,
                # Sub-agents run the native agent loop, never the DAG planner:
                # TaskExecutor.run() skips pre-flight for depth>0 tasks, and the
                # empty agent_state lets the loop seed a clean history from the goal.
                "agent_state": {"agent_history": [], "pending_agent_approval": False},
                "current_step": 0,
                "result": {},
                "depth": int(parent_task.get("depth") or 0) + 1,
            }
        )

        await emit_activity(
            parent_task["id"],
            {
                "type": "sub_agent_spawned",
                "sub_task_id": sub_task_id,
                "goal": goal,
                "depth": int(parent_task.get("depth") or 0) + 1,
            },
        )

        from runtime.executor import TaskExecutor

        asyncio.create_task(TaskExecutor().run(sub_task_id))
        return await self._forward_until_done(sub_task_id, parent_task["id"])

    async def _forward_until_done(self, sub_task_id: str, parent_task_id: str) -> dict[str, Any]:
        pubsub = redis_client.pubsub()
        channel = f"activity:{sub_task_id}"
        await pubsub.subscribe(channel)
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30)
                if not message:
                    task = await get_task(sub_task_id)
                    if task and task.get("status") == "complete":
                        return await self._summarize_result(sub_task_id, task)
                    if task and task.get("status") == "failed":
                        raise SubAgentFailed(task.get("error") or "sub-agent failed")
                    await asyncio.sleep(0)
                    continue
                event = json.loads(message["data"])
                # Forward nested events to parent's activity stream.
                await emit_activity(
                    parent_task_id,
                    {"type": "sub_agent_event", "sub_task_id": sub_task_id, "event": event},
                )
                if event.get("type") == "task_complete":
                    task = await get_task(sub_task_id)
                    return await self._summarize_result(sub_task_id, task or {})
                if event.get("type") == "task_failed":
                    raise SubAgentFailed(event.get("error", "sub-agent failed"))
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    async def _summarize_result(self, sub_task_id: str, task: dict[str, Any]) -> dict[str, Any]:
        """Return a structured summary (not the full transcript) to the parent's loop history."""
        raw = dict(task.get("result") or {})
        profile = await self._save_sub_agent_profile(sub_task_id, task)

        # Build a compact summary — what the parent model needs to continue.
        summary: dict[str, Any] = {
            "sub_task_id": sub_task_id,
            "goal": task.get("goal", ""),
            "status": "complete",
            "profile": profile,
        }
        # Surface important keys (leads, drafts, findings, answer) at the top level.
        for key in ("leads", "drafts", "findings", "answer", "summary"):
            if key in raw:
                summary[key] = raw[key]
        # Attach a token-efficient text summary if the model left one.
        if isinstance(raw.get("answer"), str):
            summary["answer"] = raw["answer"]
        return summary

    async def _save_sub_agent_profile(self, sub_task_id: str, task: dict[str, Any]) -> dict[str, Any]:
        profile = {
            "task_id": sub_task_id,
            "goal": task.get("goal", ""),
            "promotable": True,
            "result_keys": list((task.get("result") or {}).keys()),
        }
        tasks = await reflect_table("tasks")
        async with engine.begin() as conn:
            row = (
                await conn.execute(select(tasks.c.result).where(tasks.c.id == sub_task_id))
            ).first()
            existing = dict(row[0] or {}) if row else {}
            existing["sub_agent_profile"] = profile
            await conn.execute(update(tasks).where(tasks.c.id == sub_task_id).values(result=existing))
        return profile


def _actor(task: dict[str, Any]):
    from core.models import Member

    return Member(
        id=task.get("triggered_by_member_id") or "chronos",
        organization_id=task["organization_id"],
        region=task["region"],
        email="chronos@local",
        role="agent",
    )
