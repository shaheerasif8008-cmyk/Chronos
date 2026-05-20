from __future__ import annotations

import json
from typing import Any

from core.config import settings
from core.llm import complete_json
from core.exceptions import ChronosError


class PlanningError(ChronosError):
    """Raised when the planner cannot return a valid JSON execution plan."""


def _normalize_step(raw: dict[str, Any], index: int) -> dict[str, Any]:
    action = raw.get("action")
    if action not in {"think", "tool_call", "spawn_sub_agent", "approval_gate"}:
        raise PlanningError(f"Invalid step action at index {index}: {action}")

    step = {
        "id": str(raw.get("id") or f"step-{index + 1}"),
        "action": action,
        "description": str(raw.get("description") or action),
        "tool": raw.get("tool"),
        "args": raw.get("args") if isinstance(raw.get("args"), dict) else {},
        "approval_required": bool(raw.get("approval_required", action == "approval_gate")),
        "depends_on": raw.get("depends_on") if isinstance(raw.get("depends_on"), list) else [],
    }
    return step


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


async def create_plan(goal: str, context: dict[str, Any] | None, org_id: str) -> list[dict[str, Any]]:
    if settings.demo_mode:
        return _demo_plan(goal)

    prompt = f"""
You are Chronos, an enterprise autonomous task planner.
Return only JSON with a top-level "steps" array. Each step must have:
id, action (think|tool_call|spawn_sub_agent|approval_gate), description, tool, args,
approval_required, depends_on.

Organization: {org_id}
Context JSON: {json.dumps(context or {}, default=str)}
Goal: {goal}
"""
    try:
        parsed = json.loads(await complete_json(prompt))
    except Exception:
        return _demo_plan(goal)

    raw_steps = parsed.get("steps", parsed if isinstance(parsed, list) else None)
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanningError("Planner response did not include a non-empty steps array")

    return [_normalize_step(step, i) for i, step in enumerate(raw_steps) if isinstance(step, dict)]
