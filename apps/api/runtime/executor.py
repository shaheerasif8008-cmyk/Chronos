from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import insert, select, update

from core import audit, permissions, tool_broker
from core.config import settings
from core.db import engine, reflect_table
from core.models import AgentContext, Member
from core.redis import redis_client
from runtime.planner import create_plan


class TaskExecutionError(Exception):
    """Raised when a task step exhausts retries."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def activity_channel(task_id: str) -> str:
    return f"activity:{task_id}"


async def emit_activity(task_id: str, event: dict[str, Any], *, actor_id: str | None = "chronos") -> None:
    payload = {"task_id": task_id, "ts": now_utc().isoformat(), **event}
    await audit.log(
        "activity",
        actor_id,
        payload.get("type", "activity"),
        resource_type="tasks",
        resource_id=task_id,
        payload=payload,
    )
    await redis_client.publish(activity_channel(task_id), json.dumps(payload, default=str))


async def get_task(task_id: str) -> dict[str, Any] | None:
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
    return dict(row) if row else None


async def update_task(task_id: str, **values: Any) -> None:
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values))


async def insert_task(values: dict[str, Any]) -> str:
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        result = await conn.execute(insert(tasks).values(**values).returning(tasks.c.id))
        return str(result.scalar_one())


def _plan_steps(task: dict[str, Any]) -> list[dict[str, Any]]:
    plan = task.get("plan") or []
    if isinstance(plan, dict):
        plan = plan.get("steps", [])
    return plan if isinstance(plan, list) else []


def approvals_ready_for_drafting(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row["status"] == "approved" and not (row.get("action_payload") or {}).get("draft_result")
    ]


def _demo_leads() -> list[dict[str, Any]]:
    verticals = [
        "sales enablement", "revops analytics", "customer success", "usage billing", "data catalog",
        "security compliance", "contract lifecycle", "HR operations", "product analytics", "support automation",
        "cloud cost", "identity governance", "field sales", "marketing attribution", "API monitoring",
        "warehouse automation", "pipeline forecasting", "enablement coaching", "partner ops", "workflow search",
    ]
    return [
        {
            "company": f"DemoSaaS {i:02d}",
            "domain": f"demosaas{i:02d}.example.com",
            "employee_count": 75 + (i * 5 % 110),
            "stage": "Series A" if i % 2 else "Series B",
            "hiring_signal": f"Hiring account executives and SDRs for {verticals[i - 1]} expansion.",
            "personalization": f"Reference their recent {verticals[i - 1]} hiring push.",
            "score": 8 + (i % 3),
        }
        for i in range(1, 21)
    ]


def _drafts_from_leads(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    drafts = []
    for lead in leads[:20]:
        company = lead["company"]
        drafts.append(
            {
                "to": f"sales@{lead['domain']}",
                "subject": f"Helping {company} convert more sales hiring into pipeline",
                "body": (
                    f"Hi {company} team,\n\n"
                    f"I noticed {lead['personalization'].lower()} Chronos helps growing B2B SaaS teams "
                    "turn hiring signals, account research, and outbound drafting into an approval-ready workflow.\n\n"
                    "Would it be useful to compare notes on where your sales team is spending research time today?\n\n"
                    "Best,\nChronos"
                ),
                "lead": lead,
            }
        )
    return drafts


class TaskExecutor:
    async def run(self, task_id: str) -> None:
        task = await get_task(task_id)
        if not task:
            raise TaskExecutionError(f"Task not found: {task_id}")
        if not _plan_steps(task):
            await update_task(task_id, status="planning")
            plan = await create_plan(task["goal"], {"task_id": task_id}, task["organization_id"])
            await update_task(task_id, plan=plan, current_step=0, status="pending")
        await self._run_loop(task_id)

    async def resume(self, task_id: str) -> None:
        await self._run_loop(task_id)

    async def _run_loop(self, task_id: str) -> None:
        task = await get_task(task_id)
        if not task:
            raise TaskExecutionError(f"Task not found: {task_id}")

        if task["status"] in {"complete", "failed", "cancelled"}:
            return

        await update_task(task_id, status="running", started_at=task.get("started_at") or now_utc(), error=None)
        steps = _plan_steps(task)
        current_step = int(task.get("current_step") or 0)

        while current_step < len(steps):
            task = await get_task(task_id)
            if not task:
                raise TaskExecutionError(f"Task not found: {task_id}")
            current_step = int(task.get("current_step") or current_step)
            step = steps[current_step]
            await update_task(task_id, current_step=current_step)
            await emit_activity(task_id, {"type": "step_start", "step_index": current_step, "step": step})

            try:
                result = await self._execute_with_retries(task, step)
            except _PausedForApproval:
                return
            except Exception as exc:
                await update_task(task_id, status="failed", error=str(exc), completed_at=now_utc())
                await emit_activity(task_id, {"type": "task_failed", "error": str(exc)})
                return

            merged = dict(task.get("result") or {})
            if result is not None:
                merged[step["id"]] = result
                if isinstance(result, dict):
                    if "leads" in result:
                        merged["leads"] = result["leads"]
                    if "drafts" in result:
                        merged["drafts"] = result["drafts"]
                    data = result.get("data")
                    if isinstance(data, dict):
                        if "leads" in data:
                            merged["leads"] = data["leads"]
                        if "drafts" in data:
                            merged["drafts"] = data["drafts"]
            current_step += 1
            await update_task(task_id, result=merged, current_step=current_step)
            await emit_activity(task_id, {"type": "step_done", "step_index": current_step - 1, "step": step, "result": result})

        final_task = await get_task(task_id) or {}
        await update_task(task_id, status="complete", completed_at=now_utc())
        await emit_activity(task_id, {"type": "task_complete", "result": final_task.get("result") or {}})

    async def _execute_with_retries(self, task: dict[str, Any], step: dict[str, Any]) -> dict[str, Any] | None:
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                return await self._execute_step(task, step)
            except _PausedForApproval:
                raise
            except Exception as exc:
                last_exc = exc
                await emit_activity(
                    task["id"],
                    {"type": "step_retry", "step": step, "attempt": attempt, "error": str(exc)},
                )
                await asyncio.sleep(0)
        raise TaskExecutionError(f"Step {step.get('id')} failed after 3 attempts: {last_exc}")

    async def _execute_step(self, task: dict[str, Any], step: dict[str, Any]) -> dict[str, Any] | None:
        actor = Member(
            id=task.get("triggered_by_member_id") or "chronos",
            organization_id=task["organization_id"],
            region=task["region"],
            email="chronos@local",
            role="agent",
        )
        await permissions.check(actor, f"task_step:{step['action']}", task.get("workspace_id") or "default")

        action = step["action"]
        if action == "spawn_sub_agent":
            from runtime.sub_agent import SubAgentManager

            goal = step.get("args", {}).get("goal") or step["description"]
            return await SubAgentManager().spawn_and_wait(task, goal)

        if action == "tool_call":
            agent = AgentContext.from_task(task)
            result = await tool_broker.execute(agent, step["tool"], step.get("args") or {})
            return {"summary": result.summary, "data": result.data}

        if action == "approval_gate":
            return await self._handle_approval_gate(task, step)

        if action == "think":
            return await self._think(task, step)

        raise ValueError(f"Unsupported task action: {action}")

    async def _think(self, task: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        result = dict(task.get("result") or {})
        leads = result.get("leads") or _demo_leads()
        should_draft = "draft" in step.get("description", "").lower()
        if should_draft and (settings.demo_mode or result.get("leads")):
            return {"leads": leads, "drafts": _drafts_from_leads(leads)}
        return {"note": step.get("description", "completed")}

    async def _handle_approval_gate(self, task: dict[str, Any], step: dict[str, Any]) -> dict[str, Any] | None:
        approvals = await reflect_table("approvals")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(approvals).where(
                        approvals.c.task_id == task["id"],
                        approvals.c.step_id == step["id"],
                    )
                )
            ).mappings().all()

        if not rows:
            approval_ids = await self._create_approvals(task, step)
            await update_task(task["id"], status="awaiting_approval")
            await emit_activity(
                task["id"],
                {"type": "awaiting_approval", "approval_ids": approval_ids, "step_id": step["id"]},
            )
            raise _PausedForApproval()

        row_dicts = [dict(row) for row in rows]
        pending = [row for row in row_dicts if row["status"] == "pending"]
        rejected = [row for row in row_dicts if row["status"] == "rejected"]
        if pending:
            await update_task(task["id"], status="awaiting_approval")
            raise _PausedForApproval()

        draft_results = await self._execute_approved_drafts(task, step, approvals_ready_for_drafting(row_dicts))
        if rejected:
            return {"approved": len(row_dicts) - len(rejected), "rejected": len(rejected), "drafts_created": draft_results}

        completed_results = [
            {"approval_id": row["id"], **row["action_payload"]["draft_result"]}
            for row in row_dicts
            if (row.get("action_payload") or {}).get("draft_result")
        ]
        return {"approved": len(row_dicts), "rejected": 0, "drafts_created": completed_results + draft_results}

    async def _execute_approved_drafts(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        approvals = await reflect_table("approvals")
        draft_results = []
        async with engine.begin() as conn:
            for row in rows:
                payload = dict(row["action_payload"])
                tool = payload.pop("tool", step.get("tool") or "gmail.draft")
                payload.pop("batch_id", None)
                payload["__approved_by_gate"] = True
                result = await tool_broker.execute(AgentContext.from_task(task), tool, payload)
                draft_result = {"summary": result.summary, "data": result.data}
                await conn.execute(
                    update(approvals)
                    .where(approvals.c.id == row["id"])
                    .values(action_payload={**row["action_payload"], "draft_result": draft_result})
                )
                draft_results.append({"approval_id": row["id"], **draft_result})
        return draft_results

    async def _create_approvals(self, task: dict[str, Any], step: dict[str, Any]) -> list[str]:
        task_result = dict(task.get("result") or {})
        drafts = task_result.get("drafts") or _drafts_from_leads(task_result.get("leads") or _demo_leads())
        approvals = await reflect_table("approvals")
        expires_at = now_utc() + timedelta(hours=24)
        batch_id = f"{task['id']}:{step['id']}"
        ids: list[str] = []
        async with engine.begin() as conn:
            for draft in drafts:
                payload = {"tool": step.get("tool") or "gmail.draft", "batch_id": batch_id, **draft}
                result = await conn.execute(
                    insert(approvals)
                    .values(
                        organization_id=task["organization_id"],
                        region=task["region"],
                        task_id=task["id"],
                        step_id=step["id"],
                        action_type=step.get("tool") or "gmail.draft",
                        action_payload=payload,
                        expires_at=expires_at,
                    )
                    .returning(approvals.c.id)
                )
                ids.append(str(result.scalar_one()))
        return ids


class _PausedForApproval(Exception):
    pass
