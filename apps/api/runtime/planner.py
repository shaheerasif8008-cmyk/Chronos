from __future__ import annotations

import json
from typing import Any

from core.config import settings
from core.llm import complete_json
from core.exceptions import ChronosError


class PlanningError(ChronosError):
    """Raised when the planner cannot return a valid JSON execution plan."""


_VALID_ACTIONS = {"think", "tool_call", "spawn_sub_agent", "approval_gate", "escalate"}


def _normalize_step(raw: dict[str, Any], index: int) -> dict[str, Any]:
    action = raw.get("action")
    if action not in _VALID_ACTIONS:
        raise PlanningError(f"Invalid step action at index {index}: {action}")

    depends_on = raw.get("depends_on") if isinstance(raw.get("depends_on"), list) else []
    step = {
        "id": str(raw.get("id") or f"step-{index + 1}"),
        "action": action,
        "description": str(raw.get("description") or action),
        "tool": raw.get("tool"),
        "args": raw.get("args") if isinstance(raw.get("args"), dict) else {},
        "approval_required": bool(raw.get("approval_required", action == "approval_gate")),
        "depends_on": [str(dep) for dep in depends_on],
    }
    if raw.get("parallel_group"):
        step["parallel_group"] = str(raw["parallel_group"])
    if raw.get("output_key"):
        step["output_key"] = str(raw["output_key"])
    if isinstance(raw.get("condition"), dict):
        step["condition"] = raw["condition"]
    if raw.get("prompt"):
        step["prompt"] = str(raw["prompt"])
    if raw.get("message"):
        step["message"] = str(raw["message"])
    return step


def normalize_plan(raw_plan: Any) -> dict[str, Any]:
    """Return the category-1 DAG plan shape stored in tasks.plan."""
    if isinstance(raw_plan, dict):
        raw_steps = raw_plan.get("steps")
        context = raw_plan.get("context") if isinstance(raw_plan.get("context"), dict) else {}
    elif isinstance(raw_plan, list):
        raw_steps = raw_plan
        context = {}
    else:
        raise PlanningError("Planner response did not include a plan object or step array")

    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanningError("Planner response did not include a non-empty steps array")

    steps = [_normalize_step(step, i) for i, step in enumerate(raw_steps) if isinstance(step, dict)]
    _validate_dag(steps)
    return {"steps": steps, "context": context}


def _validate_dag(steps: list[dict[str, Any]]) -> None:
    ids = [step["id"] for step in steps]
    if len(set(ids)) != len(ids):
        raise PlanningError("Plan contains duplicate step ids")

    known = set(ids)
    for step in steps:
        missing = [dep for dep in step.get("depends_on", []) if dep not in known]
        if missing:
            raise PlanningError(f"Step {step['id']} depends on unknown steps: {', '.join(missing)}")

    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {step["id"]: step for step in steps}

    def visit(step_id: str) -> None:
        if step_id in visited:
            return
        if step_id in visiting:
            raise PlanningError("Plan dependency graph contains a cycle")
        visiting.add(step_id)
        for dep in by_id[step_id].get("depends_on", []):
            visit(dep)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in ids:
        visit(step_id)


def _demo_plan(goal: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "research",
            "action": "spawn_sub_agent",
            "description": "Research B2B SaaS companies matching the requested ICP.",
            "tool": None,
            "args": {"goal": goal},
            "approval_required": False,
            "depends_on": [],
        },
        {
            "id": "qualify_and_draft",
            "action": "think",
            "description": "Qualify leads, score fit, and prepare personalized Gmail draft payloads.",
            "tool": None,
            "args": {},
            "approval_required": False,
            "depends_on": ["research"],
        },
        {
            "id": "approve_drafts",
            "action": "approval_gate",
            "description": "Request approval for each outbound draft before Gmail drafts are created.",
            "tool": "gmail.draft",
            "args": {"from_result": "drafts"},
            "approval_required": True,
            "depends_on": ["qualify_and_draft"],
        },
    ]


def _research_plan(goal: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "research",
            "action": "tool_call",
            "description": "Search for source material relevant to the requested research brief.",
            "tool": "browser.search",
            "args": {"query": goal, "max_results": 8},
            "approval_required": False,
            "depends_on": [],
        },
        {
            "id": "synthesize",
            "action": "think",
            "description": "Summarize the research findings and compare the main players.",
            "tool": None,
            "args": {},
            "approval_required": False,
            "depends_on": ["research"],
        },
    ]


def _operator_workflow_proof_plan(goal: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "proof_search",
            "action": "tool_call",
            "description": "Use the browser connector proof fixture to produce deterministic lead research.",
            "tool": "browser.search",
            "args": {"query": goal, "max_results": 20, "fixture": "operator_workflow_proof"},
            "approval_required": False,
            "depends_on": [],
        },
        {
            "id": "proof_drafts",
            "action": "think",
            "description": "Draft approval-ready outreach from the deterministic operator workflow leads.",
            "tool": None,
            "args": {},
            "approval_required": False,
            "depends_on": ["proof_search"],
        },
        {
            "id": "proof_approval",
            "action": "approval_gate",
            "description": "Request operator approval for the proof outreach drafts.",
            "tool": "gmail.draft",
            "args": {"from_result": "drafts"},
            "approval_required": True,
            "depends_on": ["proof_drafts"],
        },
    ]


def _is_operator_workflow_proof(goal: str) -> bool:
    normalized = " ".join(goal.lower().split())
    return "operator workflow proof" in normalized


def _is_outreach_goal(goal: str) -> bool:
    normalized = goal.lower()
    return any(term in normalized for term in ("lead", "leads", "outreach", "draft", "email", "gmail", "sdr"))


def _is_research_goal(goal: str) -> bool:
    normalized = goal.lower()
    return any(term in normalized for term in ("research", "brief", "compare", "comparison", "market", "players", "analyze", "analysis"))


async def create_plan(goal: str, context: dict[str, Any] | None, org_id: str) -> dict[str, Any]:
    if _is_operator_workflow_proof(goal):
        plan = normalize_plan(_operator_workflow_proof_plan(goal))
        plan["context"]["allow_replan"] = False
        return plan

    if settings.demo_mode:
        fallback = _demo_plan(goal) if _is_outreach_goal(goal) else _research_plan(goal)
        plan = normalize_plan(fallback)
        plan["context"]["allow_replan"] = False
        return plan

    prompt = f"""
You are Chronos, an enterprise autonomous task planner.
Return only JSON with a top-level "steps" array and optional "context" object.
The steps form a directed acyclic graph. Each step must have:
id, action (think|tool_call|spawn_sub_agent|approval_gate), description, tool, args,
approval_required, depends_on.
Use parallel_group for independent steps that can run together, output_key to save
results into shared context, and condition for branches. Conditions may use simple
expressions over previous output keys, for example len(raw.results) > 0.

Organization: {org_id}
Context JSON: {json.dumps(context or {}, default=str)}
Goal: {goal}
"""
    try:
        parsed = json.loads(await complete_json(prompt, model=settings.agent_model))
    except Exception:
        fallback = _demo_plan(goal) if _is_outreach_goal(goal) else _research_plan(goal)
        plan = normalize_plan(fallback)
        plan["context"]["allow_replan"] = False
        return plan

    return normalize_plan(parsed)
