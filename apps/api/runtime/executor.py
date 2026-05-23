from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import insert, select, update

from core import audit, llm, permissions, tool_broker
from core.config import settings
from core.exceptions import ApprovalRequired
from core.db import engine, reflect_table
from core.models import AgentContext, Member
from core.redis import redis_client
from runtime.planner import create_plan


class TaskExecutionError(Exception):
    """Raised when a task step exhausts retries."""


AGENT_LOOP_APPROVAL_STEP_ID = "agent_loop"


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
        plan = plan.get("suggested_steps") or plan.get("steps", [])
    return plan if isinstance(plan, list) else []


def _task_plan(task: dict[str, Any]) -> dict[str, Any]:
    plan = task.get("plan") or {}
    if isinstance(plan, dict):
        return dict(plan)
    if isinstance(plan, list):
        return {"suggested_steps": plan, "agent_history": []}
    return {"suggested_steps": [], "agent_history": []}


def _task_agent_state(task: dict[str, Any]) -> dict[str, Any]:
    state = task.get("agent_state")
    if isinstance(state, dict):
        return dict(state)
    plan = task.get("plan")
    if isinstance(plan, dict):
        return {
            "agent_history": plan.get("agent_history") or [],
            "pending_agent_approval": bool(plan.get("pending_agent_approval")),
        }
    return {"agent_history": [], "pending_agent_approval": False}


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
    MAX_ITERATIONS = 40

    async def run(self, task_id: str) -> None:
        task = await get_task(task_id)
        if not task:
            raise TaskExecutionError(f"Task not found: {task_id}")
        if not _plan_steps(task):
            await update_task(task_id, status="planning")
            plan = await create_plan(task["goal"], {"task_id": task_id}, task["organization_id"])
            await update_task(task_id, plan=plan, agent_state={"agent_history": [], "pending_agent_approval": False}, current_step=0, status="pending")
        await self._run_loop(task_id)

    async def resume(self, task_id: str) -> None:
        await self._run_loop(task_id)

    async def _run_loop(self, task_id: str) -> None:
        task = await get_task(task_id)
        if not task:
            raise TaskExecutionError(f"Task not found: {task_id}")

        if task["status"] in {"complete", "failed", "cancelled"}:
            return

        if "agent_state" not in task and isinstance(task.get("plan"), list):
            await self._run_legacy_plan(task)
            return

        await update_task(task_id, status="running", started_at=task.get("started_at") or now_utc(), error=None)
        history = await self._seed_history(task)
        tools = await self._available_tool_schemas(task)

        while int((task.get("iteration_count") or 0)) < self.MAX_ITERATIONS:
            task = await get_task(task_id)
            if not task:
                raise TaskExecutionError(f"Task not found: {task_id}")
            if task["status"] in {"complete", "failed", "cancelled"}:
                return
            plan_state = _task_agent_state(task)
            history = plan_state.get("agent_history") or history
            iteration = int(task.get("iteration_count") or 0)

            if plan_state.get("pending_agent_approval"):
                processed_approval = await self._process_agent_loop_approvals(task, history)
                if processed_approval:
                    continue
                if await self._has_pending_agent_loop_approval(task):
                    await update_task(task_id, status="awaiting_approval")
                    return

            try:
                decision = await self._next_decision(task, history, tools)
            except _PausedForApproval:
                return
            except Exception as exc:
                try:
                    decision = await self._fallback_plan_decision(task, history)
                except _PausedForApproval:
                    return

            if decision.get("type") == "final":
                result = decision.get("result") if isinstance(decision.get("result"), dict) else {"answer": str(decision.get("result") or "")}
                history.append({"role": "assistant", "content": json.dumps(result, default=str)})
                await self._checkpoint_history(task, history, iteration + 1)
                await update_task(task_id, status="complete", result=result, completed_at=now_utc())
                await emit_activity(task_id, {"type": "task_complete", "result": result})
                return

            if decision.get("type") != "tool_call":
                await update_task(task_id, status="failed", error="Agent returned an unsupported decision", completed_at=now_utc())
                await emit_activity(task_id, {"type": "task_failed", "error": "Agent returned an unsupported decision"})
                return

            tool = self._normalize_tool_name(str(decision.get("tool") or ""))
            args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
            history.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": tool, "arguments": json.dumps(args, default=str)}}]})
            await emit_activity(task_id, {"type": "tool_call", "tool": tool, "args_preview": self._args_preview(args), "iteration": iteration + 1})
            try:
                result = await tool_broker.execute(AgentContext.from_task(task), tool, args)
            except ApprovalRequired:
                await self._open_approval_checkpoint(task, decision, history)
                return
            except Exception as exc:
                history.append({"role": "tool", "name": tool, "content": json.dumps({"error": str(exc)}, default=str)})
                await self._checkpoint_history(task, history, iteration + 1)
                await emit_activity(task_id, {"type": "tool_error", "tool": tool, "error": str(exc)})
                continue

            tool_payload = {"summary": result.summary, "data": result.data}
            history.append({"role": "tool", "name": tool, "content": json.dumps(tool_payload, default=str)})
            merged = self._merge_tool_result(task.get("result") or {}, f"iteration_{iteration + 1}", tool_payload)
            step_progress = self._step_progress_after_tool(task, tool)
            update_values: dict[str, Any] = {"result": merged}
            if step_progress is not None:
                update_values["current_step"] = step_progress
            await update_task(task_id, **update_values)
            await self._checkpoint_history(task, history, iteration + 1)
            await emit_activity(task_id, {"type": "tool_result", "tool": tool, "summary": result.summary, "iteration": iteration + 1})

        await update_task(task_id, status="failed", error="max_iterations_exceeded", completed_at=now_utc())
        await emit_activity(task_id, {"type": "task_failed", "error": "max_iterations_exceeded"})

    async def _run_legacy_plan(self, task: dict[str, Any]) -> None:
        task_id = task["id"]
        await update_task(task_id, status="running", started_at=task.get("started_at") or now_utc(), error=None)
        try:
            decision = await self._fallback_plan_decision(task, [])
        except _PausedForApproval:
            return
        if decision.get("type") == "final":
            result = decision.get("result") if isinstance(decision.get("result"), dict) else {"answer": str(decision.get("result") or "")}
            await update_task(task_id, status="complete", result=result, completed_at=now_utc())
            await emit_activity(task_id, {"type": "task_complete", "result": result})

    async def _seed_history(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        plan = _task_plan(task)
        state = _task_agent_state(task)
        history = state.get("agent_history")
        if isinstance(history, list) and history:
            return history
        suggested = _plan_steps(task)
        seed = [
            {
                "role": "system",
                "content": (
                    "You are Chronos running an autonomous enterprise task. "
                    "Pick one tool call at a time, inspect results, and adapt. "
                    "When the task is done, return a final answer. Every external action is governed by the broker."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"goal": task["goal"], "suggested_plan": suggested}, default=str),
            },
        ]
        await self._checkpoint_history(task, seed, int(task.get("iteration_count") or 0))
        return seed

    async def _available_tool_schemas(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "browser__search",
                    "description": "Search the web and return structured results.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}, "fixture": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser__fetch",
                    "description": "Fetch and parse a web page.",
                    "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser__extract_contacts",
                    "description": "Extract public contact information from a page.",
                    "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "gmail__draft",
                    "description": "Create an approved Gmail draft. Must only be called after an approval checkpoint.",
                    "parameters": {
                        "type": "object",
                        "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
                        "required": ["to", "subject", "body"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fs__list",
                    "description": "List files in the current task workspace.",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fs__read",
                    "description": "Read a UTF-8 text file from the current task workspace.",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fs__write",
                    "description": "Write a UTF-8 text file inside the current task workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "code__python",
                    "description": "Run restricted Python code in the current task workspace. Use for computation over local task artifacts, not network access.",
                    "parameters": {
                        "type": "object",
                        "properties": {"code": {"type": "string"}, "timeout_seconds": {"type": "integer"}},
                        "required": ["code"],
                    },
                },
            },
        ]

    async def _next_decision(self, task: dict[str, Any], history: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        return await llm.tool_call(history, tools, model=settings.agent_model)

    async def _fallback_plan_decision(self, task: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
        steps = _plan_steps(task)
        current_step = int(task.get("current_step") or 0)
        if current_step >= len(steps):
            return {"type": "final", "result": task.get("result") or {"status": "complete"}}
        step = steps[current_step]
        await emit_activity(task["id"], {"type": "step_start", "step_index": current_step, "step": step, "mode": "fallback"})
        try:
            result = await self._execute_with_retries(task, step)
        except _PausedForApproval:
            raise
        merged = self._merge_tool_result(task.get("result") or {}, step["id"], result or {})
        await update_task(task["id"], current_step=current_step + 1, result=merged)
        await emit_activity(task["id"], {"type": "step_done", "step_index": current_step, "step": step, "result": result})
        if current_step + 1 >= len(steps):
            return {"type": "final", "result": merged}
        return await self._fallback_plan_decision({**task, "current_step": current_step + 1, "result": merged}, history)

    async def _checkpoint_history(self, task: dict[str, Any], history: list[dict[str, Any]], iteration_count: int) -> None:
        state = _task_agent_state(task)
        state["agent_history"] = history
        values: dict[str, Any] = {"agent_state": state, "iteration_count": iteration_count}
        if task.get("agent_state") is None and isinstance(task.get("plan"), dict):
            values["plan"] = {**task["plan"], "agent_history": history}
        await update_task(task["id"], **values)

    async def _open_approval_checkpoint(self, task: dict[str, Any], decision: dict[str, Any], history: list[dict[str, Any]]) -> None:
        approvals = await reflect_table("approvals")
        tool = self._normalize_tool_name(str(decision.get("tool") or ""))
        args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
        args_hash = self._args_hash(args)

        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(approvals).where(
                        approvals.c.task_id == task["id"],
                        approvals.c.step_id == AGENT_LOOP_APPROVAL_STEP_ID,
                        approvals.c.status == "pending",
                    )
                )
            ).mappings().all()
            approval_ids = [
                str(row["id"])
                for row in rows
                if (row.get("action_payload") or {}).get("args_hash") == args_hash
                and (row.get("action_payload") or {}).get("tool") == tool
            ]
            if not approval_ids:
                payload = {
                    "tool": tool,
                    "args": args,
                    "args_hash": args_hash,
                    "agent_loop": True,
                    "justification": decision.get("justification") or f"Chronos requested permission to run {tool}.",
                }
                result = await conn.execute(
                    insert(approvals)
                    .values(
                        organization_id=task["organization_id"],
                        region=task["region"],
                        task_id=task["id"],
                        step_id=AGENT_LOOP_APPROVAL_STEP_ID,
                        action_type=tool,
                        action_payload=payload,
                        expires_at=now_utc() + timedelta(hours=24),
                    )
                    .returning(approvals.c.id)
                )
                approval_ids = [str(result.scalar_one())]

        state = _task_agent_state(task)
        state["agent_history"] = history
        state["pending_agent_approval"] = True
        values: dict[str, Any] = {"agent_state": state, "iteration_count": int(task.get("iteration_count") or 0) + 1}
        if task.get("agent_state") is None and isinstance(task.get("plan"), dict):
            values["plan"] = {**task["plan"], "agent_history": history, "pending_agent_approval": True}
        await update_task(task["id"], **values)
        await update_task(task["id"], status="awaiting_approval")
        await emit_activity(
            task["id"],
            {
                "type": "awaiting_approval",
                "approval_ids": approval_ids,
                "step_id": AGENT_LOOP_APPROVAL_STEP_ID,
                "tool": tool,
                "args_preview": self._args_preview(args),
            },
        )
        raise _PausedForApproval()

    async def _process_agent_loop_approvals(self, task: dict[str, Any], history: list[dict[str, Any]]) -> bool:
        approvals = await reflect_table("approvals")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(approvals).where(
                        approvals.c.task_id == task["id"],
                        approvals.c.step_id == AGENT_LOOP_APPROVAL_STEP_ID,
                    )
                )
            ).mappings().all()

        processed = False
        for row in [dict(row) for row in rows]:
            payload = dict(row.get("action_payload") or {})
            if payload.get("execution_result") or payload.get("execution_error") or payload.get("rejection_result"):
                continue
            status = row.get("status")
            if status not in {"approved", "rejected"}:
                continue

            tool = self._normalize_tool_name(str(payload.get("tool") or row.get("action_type") or ""))
            if status == "rejected":
                rejection = {"status": "rejected", "approval_id": row["id"], "reason": row.get("decision_note")}
                history.append({"role": "tool", "name": tool, "content": json.dumps(rejection, default=str)})
                await self._mark_agent_loop_approval(row["id"], {**payload, "rejection_result": rejection})
                await emit_activity(task["id"], {"type": "approval_rejected", "approval_id": row["id"], "tool": tool})
                processed = True
                continue

            args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
            try:
                result = await tool_broker.execute(AgentContext.from_task(task), tool, {**args, "__approved_by_gate": True})
            except Exception as exc:
                error = {"status": "failed", "approval_id": row["id"], "error": str(exc)}
                history.append({"role": "tool", "name": tool, "content": json.dumps(error, default=str)})
                await self._mark_agent_loop_approval(row["id"], {**payload, "execution_error": error})
                await emit_activity(task["id"], {"type": "tool_error", "tool": tool, "approval_id": row["id"], "error": str(exc)})
                processed = True
                continue

            tool_payload = {"summary": result.summary, "data": result.data, "approval_id": row["id"]}
            history.append({"role": "tool", "name": tool, "content": json.dumps(tool_payload, default=str)})
            merged = self._merge_tool_result(task.get("result") or {}, f"approval_{row['id']}", tool_payload)
            await update_task(task["id"], result=merged)
            await self._mark_agent_loop_approval(row["id"], {**payload, "execution_result": tool_payload})
            await emit_activity(task["id"], {"type": "tool_result", "tool": tool, "approval_id": row["id"], "summary": result.summary})
            processed = True

        if processed:
            refreshed = await get_task(task["id"]) or task
            await self._checkpoint_history(refreshed, history, int(refreshed.get("iteration_count") or 0) + 1)
            state = _task_agent_state(refreshed)
            state["agent_history"] = history
            state["pending_agent_approval"] = False
            await update_task(task["id"], agent_state=state)
            await update_task(task["id"], status="running")
        return processed

    async def _has_pending_agent_loop_approval(self, task: dict[str, Any]) -> bool:
        approvals = await reflect_table("approvals")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(approvals.c.id).where(
                        approvals.c.task_id == task["id"],
                        approvals.c.step_id == AGENT_LOOP_APPROVAL_STEP_ID,
                        approvals.c.status == "pending",
                    )
                )
            ).first()
        return row is not None

    async def _mark_agent_loop_approval(self, approval_id: str, payload: dict[str, Any]) -> None:
        approvals = await reflect_table("approvals")
        async with engine.begin() as conn:
            await conn.execute(update(approvals).where(approvals.c.id == approval_id).values(action_payload=payload))

    def _normalize_tool_name(self, tool: str) -> str:
        if "__" in tool and "." not in tool:
            return tool.replace("__", ".", 1)
        return tool

    def _args_hash(self, args: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()

    def _step_progress_after_tool(self, task: dict[str, Any], tool: str) -> int | None:
        steps = _plan_steps(task)
        current_step = int(task.get("current_step") or 0)
        if current_step >= len(steps):
            return None
        step = steps[current_step]
        if step.get("action") == "tool_call" and step.get("tool") == tool:
            return current_step + 1
        return None

    def _args_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        return {key: ("[omitted]" if key.lower() in {"body", "content"} else value) for key, value in args.items()}

    def _merge_tool_result(self, existing: dict[str, Any], key: str, result: dict[str, Any]) -> dict[str, Any]:
        merged = dict(existing)
        merged[key] = result
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
        return merged

    async def _execute_with_retries(self, task: dict[str, Any], step: dict[str, Any]) -> dict[str, Any] | None:
        last_exc: Exception | None = None
        max_attempts = 1 if step.get("action") == "spawn_sub_agent" else 3
        for attempt in range(1, max_attempts + 1):
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
        raise TaskExecutionError(f"Step {step.get('id')} failed after {max_attempts} attempts: {last_exc}")

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
        research = self._research_findings(result)
        if research:
            return research
        return {"note": step.get("description", "completed")}

    def _research_findings(self, result: dict[str, Any]) -> dict[str, Any] | None:
        search = result.get("research")
        if isinstance(search, dict):
            data = search.get("data") if isinstance(search.get("data"), dict) else {}
            raw_results = data.get("results") or data.get("leads") or []
        else:
            raw_results = result.get("results") or []
        if not isinstance(raw_results, list) or not raw_results:
            return None

        findings = []
        for item in raw_results[:5]:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("company") or item.get("url") or "Research result"
            snippet = item.get("snippet") or item.get("hiring_signal") or item.get("content") or ""
            findings.append(
                {
                    "title": title,
                    "summary": snippet,
                    "url": item.get("url"),
                }
            )
        if not findings:
            return None
        return {
            "findings": findings,
            "summary": "Compiled research findings from the browser search results.",
        }

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
