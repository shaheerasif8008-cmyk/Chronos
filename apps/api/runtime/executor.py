"""
Task Executor — compatibility shim over the native agent loop.

All external imports that other modules depend on are preserved here:
    TaskExecutor, activity_channel, emit_activity, get_task, update_task,
    insert_task, approvals_ready_for_drafting, AGENT_LOOP_APPROVAL_STEP_ID.

Heavy lifting has moved to runtime/agent_loop.py.
"""
from __future__ import annotations

import asyncio
import ast
import inspect
import json
import logging
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

log = logging.getLogger(__name__)

from sqlalchemy import insert, select, update

from core import tool_broker
from core.db import engine, reflect_table
from core.llm import complete_json
from core.models import AgentContext
from runtime.agent_loop import (
    activity_channel,
    emit_activity,
    get_task,
    run_loop,
    resume_after_approval,
    save_task,
)
from runtime.checkpoints import create_checkpoint
from runtime.planner import (
    PlanningError,
    create_plan,
    default_available_tools,
    normalize_plan,
    preflight,
    validate_plan,
)

# ── Public name kept for imports in approvals.py, sub_agent.py, tests ─────────
AGENT_LOOP_APPROVAL_STEP_ID = "agent_loop"

# Sentinel returned by _preflight_and_route when the task was failed pre-flight.
_PREFLIGHT_FAILED = object()


class _PausedForApproval(Exception):
    """Internal control flow for DAG steps that created approval records."""


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
    """Run category-1 DAG plans, falling back to the native loop for empty plans."""

    async def run(self, task_id: str) -> None:
        task = await get_task(task_id)
        if not task:
            raise RuntimeError(f"Task not found: {task_id}")
        if task["status"] in {"complete", "failed", "cancelled"}:
            return
        initial_step = int(task.get("current_step") or 0)
        try:
            await save_task(task_id, status="running", started_at=now_utc())
            if _has_dag_plan(task.get("plan")):
                await self._run_dag(task)
                return
            if _is_fresh_top_level(task):
                routed = await self._preflight_and_route(task)
                if routed is _PREFLIGHT_FAILED:
                    return
                if routed is not None:
                    await self._run_dag(routed)
                    return
            await run_loop(task)
        except asyncio.CancelledError:
            await save_task(task_id, status="cancelled", error="Task execution was cancelled.", completed_at=now_utc())
            raise
        except Exception as exc:
            refreshed = await get_task(task_id)
            if refreshed and refreshed.get("status") not in {"complete", "failed", "cancelled"}:
                refreshed_step = int(refreshed.get("current_step") or 0)
                if refreshed_step > initial_step:
                    log.exception("Task %s interrupted after checkpoint; leaving it resumable", task_id)
                    raise
            error = f"executor_error: {type(exc).__name__}: {exc}"
            log.exception("Task %s crashed in executor", task_id)
            await save_task(task_id, status="failed", error=error, completed_at=now_utc())
            await emit_activity(task_id, {"type": "task_failed", "error": error})
            raise

    async def _preflight_and_route(self, task: dict[str, Any]) -> Any:
        """Category-4 pre-flight plus category-1 routing for fresh top-level tasks.

        Classifies the goal, dispatches complex goals to the DAG planner, and lets
        everything else fall through to the native loop. Returns:
          * ``_PREFLIGHT_FAILED`` — the task was failed (required a missing tool),
          * a task dict with ``plan`` populated — a DAG plan was built (complex goal),
          * ``None`` — use the native agent loop (simple/medium goal).

        A classifier or planner failure never blocks execution: it falls back to the
        native loop rather than failing the task.
        """
        goal = str(task.get("goal") or "")
        context = {"triggered_by": task.get("triggered_by")}
        org_id = task["organization_id"]
        try:
            classification, missing = await preflight(goal, context, org_id)
        except Exception:
            return None
        if missing:
            error = (
                f"missing_tools: {', '.join(missing)} — "
                "connect the required integrations in Settings → Connectors."
            )
            await save_task(task["id"], status="failed", error=error, result={}, completed_at=now_utc())
            await emit_activity(task["id"], {"type": "task_failed", "error": error})
            return _PREFLIGHT_FAILED
        if classification.complexity != "complex":
            return None
        try:
            plan = await create_plan(goal, context, org_id)
        except PlanningError as exc:
            await emit_activity(task["id"], {"type": "planner_fallback", "error": str(exc)})
            return None
        await save_task(task["id"], plan=plan)
        return {**task, "plan": plan}

    async def resume(self, task_id: str) -> None:
        """Resume an interrupted task — after an approval pause or a process crash.

        Called both by the approvals router and by ``recover_incomplete_tasks`` on
        startup, which sweeps every ``pending``/``planning``/``running`` task. Routing:

          * DAG plan present        → ``_resume_dag`` (resumes from completed_step_ids).
          * awaiting_approval/paused → ``resume_after_approval`` (existing behaviour).
          * native loop with history → re-enter ``run_loop`` from the checkpointed
            ``agent_history``/``iteration_count`` (this is the crash-resume path that
            was previously dropped — ``resume_after_approval`` no-ops on ``running``).
          * never really started     → delegate to ``run``, which is idempotent: it
            short-circuits on terminal status and re-runs pre-flight + DAG routing.
        """
        task = await get_task(task_id)
        if not task:
            return
        if task["status"] in {"complete", "failed", "cancelled"}:
            return
        if _has_dag_plan(task.get("plan")):
            await self._resume_dag(task)
            return
        if task["status"] in {"awaiting_approval", "paused"}:
            await resume_after_approval(task_id)
            return
        state = task.get("agent_state") or {}
        if isinstance(state, dict) and state.get("agent_history"):
            await run_loop(task)
        else:
            await self.run(task_id)

    async def _run_dag(self, task: dict[str, Any]) -> dict[str, Any]:
        task_id = task["id"]
        plan = normalize_plan(task.get("plan"))
        validation = validate_plan(plan, default_available_tools())
        if not validation.valid:
            error = f"invalid_plan: {'; '.join(validation.errors)}"
            await save_task(
                task_id,
                status="failed",
                error=error,
                result={},
                completed_at=datetime.now(timezone.utc),
            )
            await emit_activity(task_id, {"type": "task_failed", "error": error})
            return {"error": error}
        state = _dag_state(task)
        completed: set[str] = set(state.get("completed_step_ids") or [])
        skipped: set[str] = set(state.get("skipped_step_ids") or [])
        context = dict(plan.get("context") or {})
        context.update(task.get("result") or {})
        model = _stored_model(task)
        agent = AgentContext.from_task(task)

        await save_task(
            task_id,
            status="running",
            plan=plan,
            started_at=task.get("started_at") or datetime.now(timezone.utc),
        )

        while True:
            if await _task_cancelled(task_id):
                await save_task(
                    task_id,
                    status="cancelled",
                    error="task_cancelled",
                    completed_at=datetime.now(timezone.utc),
                    agent_state=_updated_dag_state(task, completed, skipped),
                )
                await emit_activity(task_id, {"type": "task_cancelled", "reason": "task_cancelled"})
                return {"error": "task_cancelled"}

            ready = self._ready_steps(plan["steps"], completed | skipped, context)
            if not ready:
                blocked = [
                    step["id"]
                    for step in plan["steps"]
                    if step["id"] not in completed and step["id"] not in skipped
                ]
                if blocked:
                    error = f"blocked_steps:{','.join(blocked)}"
                    await save_task(task_id, status="failed", error=error, result=_public_context(context))
                    await emit_activity(task_id, {"type": "task_failed", "error": error})
                    return {"error": error}
                result = _public_context(context)
                await save_task(
                    task_id,
                    status="complete",
                    current_step=len(completed),
                    result=result,
                    completed_at=datetime.now(timezone.utc),
                    agent_state=_updated_dag_state(task, completed, skipped),
                )
                await emit_activity(task_id, {"type": "task_complete", "result": result})
                return result

            runnable: list[dict[str, Any]] = []
            for step in ready:
                if not self._condition_met(step, context):
                    skipped.add(step["id"])
                    await emit_activity(task_id, {"type": "step_skipped", "step": step, "reason": "condition_false"})
                    continue
                runnable.append(step)

            if not runnable:
                await self._checkpoint_dag(task, plan, completed, skipped, context)
                continue

            results = await asyncio.gather(
                *[self._execute_dag_step(task, step, context, agent, model, completed, skipped) for step in runnable],
                return_exceptions=True,
            )
            for step, result in zip(runnable, results):
                if isinstance(result, _PausedForApproval):
                    return {"status": "awaiting_approval"}
                if isinstance(result, Exception):
                    error = str(result)
                    await save_task(
                        task_id,
                        status="failed",
                        error=error,
                        result=_public_context(context),
                        completed_at=datetime.now(timezone.utc),
                        agent_state=_updated_dag_state(task, completed, skipped),
                    )
                    await emit_activity(task_id, {"type": "task_failed", "error": error, "step": step})
                    return {"error": error}

                completed.add(step["id"])
                output_key = step.get("output_key") or step["id"]
                if result is not None:
                    context[output_key] = result
                    if isinstance(result, dict):
                        for key in ("leads", "drafts", "findings", "answer", "summary"):
                            if key in result:
                                context[key] = result[key]
                await self._checkpoint_dag(task, plan, completed, skipped, context)
                await emit_activity(task_id, {"type": "step_done", "step": step, "output_key": output_key})
                if step.get("checkpoint"):
                    await create_checkpoint(task, step["checkpoint"], _public_context(context), len(completed))

            await self._checkpoint_dag(task, plan, completed, skipped, context)
            revised_remaining = await _maybe_await(self._maybe_replan(task, completed, context, model))
            if revised_remaining:
                plan = self._merge_replan(plan, completed, skipped, revised_remaining)
                await save_task(task_id, plan=plan)

    def _ready_steps(
        self,
        steps: list[dict[str, Any]],
        resolved: set[str],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        del context
        return [
            step
            for step in steps
            if step["id"] not in resolved
            and all(dep in resolved for dep in step.get("depends_on", []))
        ]

    async def _execute_dag_step(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        context: dict[str, Any],
        agent: AgentContext,
        model: str | None,
        completed: set[str],
        skipped: set[str],
    ) -> Any:
        await emit_activity(task["id"], {"type": "step_start", "step": step})
        action = step["action"]
        if action == "tool_call":
            args = resolve_args(dict(step.get("args") or {}), context)
            result = await tool_broker.execute(agent, str(step["tool"]), args)
            return result.data
        if action == "think":
            return await self._run_think_step(task, step, context, model)
        if action == "escalate":
            raise RuntimeError(step.get("message") or step.get("description") or "Escalation required")
        if action == "approval_gate":
            await self._handle_approval_gate(task, step, context, completed, skipped)
            raise _PausedForApproval()
        if action == "spawn_sub_agent":
            from runtime.agent_loop import _run_subagent

            args = dict(step.get("args") or {})
            # Step 4: opt-in state inheritance. The spawn step's `inherit_keys` is the
            # canonical source; resolve it against the LIVE context (not the stale
            # persisted result) and hand the snapshot to the child.
            inherit_keys = args.get("inherit_keys")
            if inherit_keys:
                args["_inherited_context"] = {
                    "parent_goal": task.get("goal", ""),
                    "parent_context": {key: context[key] for key in inherit_keys if key in context},
                }
            return await _run_subagent(task, args, int(task.get("depth") or 0))
        raise RuntimeError(f"Unsupported DAG step action: {action}")

    async def _run_think_step(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        context: dict[str, Any],
        model: str | None,
    ) -> dict[str, Any]:
        prompt = step.get("prompt") or step.get("description") or "Reason over the task context."
        if _find_context_list(context, "leads") and "draft" in f"{step.get('id', '')} {prompt}".lower():
            return self._deterministic_think(step, context)
        try:
            raw = await complete_json(
                "Return JSON only.\n"
                f"Task goal: {task.get('goal')}\n"
                f"Step: {prompt}\n"
                f"Context: {json.dumps(_public_context(context), default=str)}",
                model=model,
            )
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            return self._deterministic_think(step, context)

    def _deterministic_think(self, step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        leads = _find_context_list(context, "leads")
        if leads:
            drafts = []
            for lead in leads[:10]:
                if not isinstance(lead, dict):
                    continue
                company = lead.get("company") or lead.get("title") or "there"
                domain = lead.get("domain") or lead.get("url") or ""
                to = lead.get("email") or (f"hello@{domain}" if domain else "")
                personalization = lead.get("personalization") or lead.get("summary") or lead.get("snippet") or ""
                drafts.append(
                    {
                        "to": to,
                        "subject": f"Quick note for {company}",
                        "body": f"Hi {company},\n\n{personalization}\n\nWould it be useful to compare notes this week?",
                        "lead": lead,
                    }
                )
            return {"drafts": drafts, "summary": f"Prepared {len(drafts)} approval-ready drafts."}
        findings = _find_context_list(context, "results") or _find_context_list(context, "findings")
        if findings:
            return {"findings": findings, "summary": f"Summarized {len(findings)} findings."}
        return {"summary": step.get("description") or "Reasoning step complete."}

    async def _handle_approval_gate(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        context: dict[str, Any],
        completed: set[str],
        skipped: set[str],
    ) -> None:
        approvals = await reflect_table("approvals")
        approval_ids: list[str] = []
        tool = str(step.get("tool") or "approval.required")
        drafts = _find_context_list(context, str((step.get("args") or {}).get("from_result") or "drafts"))
        payloads = drafts if drafts else [dict(step.get("args") or {})]

        async with engine.begin() as conn:
            for index, payload in enumerate(payloads):
                row = await conn.execute(
                    insert(approvals)
                    .values(
                        organization_id=task["organization_id"],
                        region=task.get("region", "us"),
                        task_id=task["id"],
                        step_id=step["id"],
                        action_type=tool,
                        action_payload={
                            **(payload if isinstance(payload, dict) else {"value": payload}),
                            "tool": tool,
                            "dag_step_id": step["id"],
                            "batch_index": index,
                        },
                    )
                    .returning(approvals.c.id)
                )
                approval_ids.append(str(row.scalar_one()))

        await save_task(
            task["id"],
            status="awaiting_approval",
            result=_public_context(context),
            agent_state=_updated_dag_state(task, completed, skipped),
        )
        await emit_activity(
            task["id"],
            {"type": "awaiting_approval", "approval_ids": approval_ids, "step_id": step["id"], "tool": tool},
        )

    async def _resume_dag(self, task: dict[str, Any]) -> None:
        if task.get("status") != "awaiting_approval":
            await self._run_dag(task)
            return

        approvals = await reflect_table("approvals")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(select(approvals).where(approvals.c.task_id == task["id"]))
            ).mappings().all()
        rows = [dict(row) for row in rows]
        if any(row["status"] == "pending" for row in rows):
            return

        state = _dag_state(task)
        completed = set(state.get("completed_step_ids") or [])
        skipped = set(state.get("skipped_step_ids") or [])
        context = dict(task.get("result") or {})
        agent = AgentContext.from_task(task)
        approval_results: list[dict[str, Any]] = []

        for row in rows:
            payload = dict(row.get("action_payload") or {})
            if payload.get("execution_result") or payload.get("execution_error") or payload.get("rejection_result"):
                continue
            step_id = str(payload.get("dag_step_id") or row["step_id"])
            if row["status"] == "rejected":
                await self._mark_approval_payload(row["id"], {**payload, "rejection_result": True}, approvals)
                skipped.add(step_id)
                continue

            tool = payload.get("tool") or row["action_type"]
            args = {
                key: value
                for key, value in payload.items()
                if key not in {"tool", "dag_step_id", "batch_index", "execution_result", "execution_error"}
            }
            try:
                result = await tool_broker.execute(agent, tool, {**args, "__approved_by_gate": True})
                result_data = {"summary": result.summary, "data": result.data}
                approval_results.append(result_data)
                await self._mark_approval_payload(row["id"], {**payload, "execution_result": result_data}, approvals)
            except Exception as exc:
                error_data = {"error": str(exc)}
                approval_results.append(error_data)
                await self._mark_approval_payload(row["id"], {**payload, "execution_error": str(exc)}, approvals)
            completed.add(step_id)

        context["approval_results"] = approval_results
        await save_task(
            task["id"],
            status="running",
            result=_public_context(context),
            agent_state=_updated_dag_state(task, completed, skipped),
        )
        refreshed = await get_task(task["id"])
        if refreshed:
            await self._run_dag(refreshed)

    async def _mark_approval_payload(self, approval_id: str, payload: dict[str, Any], approvals_table: Any) -> None:
        async with engine.begin() as conn:
            await conn.execute(
                update(approvals_table).where(approvals_table.c.id == approval_id).values(action_payload=payload)
            )

    async def _maybe_replan(
        self,
        task: dict[str, Any],
        completed_steps: set[str],
        context: dict[str, Any],
        model: str | None,
    ) -> list[dict[str, Any]] | None:
        plan = normalize_plan(task.get("plan"))
        if plan.get("context", {}).get("allow_replan") is False:
            return None
        remaining = [step for step in plan["steps"] if step["id"] not in completed_steps]
        if not remaining:
            return None
        try:
            raw = await complete_json(
                "You are Chronos revising a DAG execution plan. Return JSON only with either "
                '{"replan": false} or {"replan": true, "steps": [...]} for the remaining DAG steps.\n'
                f"Goal: {task.get('goal')}\n"
                f"Completed step ids: {sorted(completed_steps)}\n"
                f"Context: {json.dumps(_public_context(context), default=str)}\n"
                f"Remaining steps: {json.dumps(remaining, default=str)}",
                model=model,
            )
            parsed = json.loads(raw)
        except Exception:
            return None
        if not isinstance(parsed, dict) or parsed.get("replan") is not True:
            return None
        steps = parsed.get("steps")
        return steps if isinstance(steps, list) and steps else None

    def _merge_replan(
        self,
        plan: dict[str, Any],
        completed: set[str],
        skipped: set[str],
        revised_remaining: list[dict[str, Any]],
    ) -> dict[str, Any]:
        fixed = [step for step in plan["steps"] if step["id"] in completed or step["id"] in skipped]
        return normalize_plan({"steps": fixed + revised_remaining, "context": plan.get("context") or {}})

    async def _checkpoint_dag(
        self,
        task: dict[str, Any],
        plan: dict[str, Any],
        completed: set[str],
        skipped: set[str],
        context: dict[str, Any],
    ) -> None:
        await save_task(
            task["id"],
            plan=plan,
            current_step=len(completed),
            result=_public_context(context),
            agent_state=_updated_dag_state(task, completed, skipped),
        )

    def _condition_met(self, step: dict[str, Any], context: dict[str, Any]) -> bool:
        condition = step.get("condition")
        if not isinstance(condition, dict) or not condition.get("if"):
            return True
        return _safe_condition(str(condition["if"]), context)


def _is_fresh_top_level(task: dict[str, Any]) -> bool:
    """A depth-0 task that has not started a native loop yet — eligible for pre-flight.

    Sub-agents (depth > 0) keep using the native loop. A task that already has agent
    history is mid-flight or resuming and must not be re-planned.
    """
    if int(task.get("depth") or 0) != 0:
        return False
    state = task.get("agent_state") or {}
    history = state.get("agent_history") if isinstance(state, dict) else None
    return not history


def _has_dag_plan(plan: Any) -> bool:
    if isinstance(plan, dict):
        return isinstance(plan.get("steps"), list) and bool(plan["steps"])
    if isinstance(plan, list):
        return bool(plan)
    return False


def _dag_state(task: dict[str, Any]) -> dict[str, Any]:
    state = task.get("agent_state") or {}
    return state.get("dag_state") if isinstance(state.get("dag_state"), dict) else {}


def _updated_dag_state(task: dict[str, Any], completed: set[str], skipped: set[str]) -> dict[str, Any]:
    state = dict(task.get("agent_state") or {})
    state["dag_state"] = {
        "completed_step_ids": sorted(completed),
        "skipped_step_ids": sorted(skipped),
    }
    return state


def _stored_model(task: dict[str, Any]) -> str | None:
    state = task.get("agent_state") or {}
    return state.get("model") if isinstance(state, dict) else None


def _public_context(context: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in context.items() if not str(k).startswith("__")}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _task_cancelled(task_id: str) -> bool:
    try:
        task = await get_task(task_id)
    except Exception:
        return False
    return bool(task and task.get("status") == "cancelled")


def _safe_condition(expression: str, context: dict[str, Any]) -> bool:
    names = {key: _to_namespace(value) for key, value in context.items() if key.isidentifier()}
    try:
        tree = ast.parse(expression, mode="eval")
        return bool(_eval_condition_node(tree.body, names))
    except Exception:
        return False


def _eval_condition_node(node: ast.AST, names: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        raise ValueError(f"unknown name: {node.id}")
    if isinstance(node, ast.Attribute):
        return getattr(_eval_condition_node(node.value, names), node.attr)
    if isinstance(node, ast.Subscript):
        value = _eval_condition_node(node.value, names)
        index = _eval_condition_node(node.slice, names)
        return value[index]
    if isinstance(node, ast.List):
        return [_eval_condition_node(item, names) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_condition_node(item, names) for item in node.elts)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_condition_node(node.operand, names)
    if isinstance(node, ast.BoolOp):
        values = [_eval_condition_node(value, names) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
    if isinstance(node, ast.Compare):
        left = _eval_condition_node(node.left, names)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_condition_node(comparator, names)
            if not _compare_values(left, op, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        args = [_eval_condition_node(arg, names) for arg in node.args]
        if node.func.id == "len" and len(args) == 1:
            return len(args[0])
        if node.func.id == "any" and len(args) == 1:
            return any(args[0])
        if node.func.id == "all" and len(args) == 1:
            return all(args[0])
    raise ValueError(f"unsupported condition expression: {ast.dump(node)}")


def _compare_values(left: Any, op: ast.cmpop, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    if isinstance(op, ast.In):
        return left in right
    if isinstance(op, ast.NotIn):
        return left not in right
    return False


def _to_namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in value.items() if str(k).isidentifier()})
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value


def _find_context_list(context: dict[str, Any], key: str) -> list[Any]:
    direct = context.get(key)
    if isinstance(direct, list):
        return direct
    for value in context.values():
        if isinstance(value, dict) and isinstance(value.get(key), list):
            return value[key]
    return []


_TEMPLATE_RE = re.compile(r"^\{\{\s*([^}]+?)\s*\}\}$")


def resolve_args(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return {key: _resolve_value(value, context) for key, value in args.items()}


def _resolve_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, context) for item in value]
    if not isinstance(value, str):
        return value
    match = _TEMPLATE_RE.match(value)
    if not match:
        return value
    return _lookup_context_path(match.group(1), context)


def _lookup_context_path(path: str, context: dict[str, Any]) -> Any:
    current: Any = context
    for part in path.split("."):
        part = part.strip()
        if not part:
            continue
        while "[" in part:
            name, _, rest = part.partition("[")
            if name:
                current = _lookup_key(current, name)
            index_text, _, remainder = rest.partition("]")
            current = current[int(index_text)]
            part = remainder
        if part:
            current = _lookup_key(current, part)
    return current


def _lookup_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value[key]
    return getattr(value, key)
