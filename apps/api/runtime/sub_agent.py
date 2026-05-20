from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import select, update

from core import audit, permissions
from core.db import engine, reflect_table
from core.exceptions import ChronosError
from core.redis import redis_client
from runtime.executor import emit_activity, get_task, insert_task


class DepthLimitExceeded(ChronosError):
    """Raised before INSERT when a sub-agent spawn would exceed the recursion limit."""


class SubAgentFailed(ChronosError):
    """Raised when a child task fails."""


class SubAgentManager:
    async def spawn_and_wait(self, parent_task: dict[str, Any], goal: str) -> dict[str, Any]:
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
                    resource_type="tasks",
                    resource_id=parent_task["id"],
                    decision="denied",
                )
                raise DepthLimitExceeded("Max sub-agent depth (3) reached")

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
                "plan": [
                    {
                        "id": "research",
                        "action": "tool_call",
                        "description": "Search for candidate companies matching the delegated goal.",
                        "tool": "browser.search",
                        "args": {"query": goal, "max_results": 20},
                        "approval_required": False,
                        "depends_on": [],
                    }
                ],
                "current_step": 0,
                "result": {},
                "depth": int(parent_task.get("depth") or 0) + 1,
            }
        )

        await emit_activity(
            parent_task["id"],
            {"type": "sub_agent_spawned", "sub_task_id": sub_task_id, "goal": goal, "depth": int(parent_task.get("depth") or 0) + 1},
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
                        profile = await self._save_sub_agent_profile(sub_task_id)
                        result = dict(task.get("result") or {})
                        result["sub_agent_profile"] = profile
                        return result
                    if task and task.get("status") == "failed":
                        raise SubAgentFailed(task.get("error") or "sub-agent failed")
                    await asyncio.sleep(0)
                    continue
                event = json.loads(message["data"])
                await emit_activity(
                    parent_task_id,
                    {"type": "sub_agent_event", "sub_task_id": sub_task_id, "event": event},
                )
                if event.get("type") == "task_complete":
                    profile = await self._save_sub_agent_profile(sub_task_id)
                    result = dict(event.get("result") or {})
                    result["sub_agent_profile"] = profile
                    return result
                if event.get("type") == "task_failed":
                    raise SubAgentFailed(event.get("error", "sub-agent failed"))
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    async def _save_sub_agent_profile(self, sub_task_id: str) -> dict[str, Any]:
        task = await get_task(sub_task_id)
        profile = {
            "task_id": sub_task_id,
            "goal": task["goal"] if task else "",
            "promotable": True,
            "result_keys": list((task.get("result") or {}).keys()) if task else [],
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
