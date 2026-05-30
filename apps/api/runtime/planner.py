from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from core.config import settings
from core.llm import complete_json
from core.exceptions import ChronosError
from runtime.tool_registry import ALL_TOOLS, ALWAYS_APPROVAL_TOOL_NAMES, to_broker_name, tool_name


class PlanningError(ChronosError):
    """Raised when the planner cannot return a valid JSON execution plan."""


@dataclass
class TaskClassification:
    complexity: str = "medium"
    requires_tools: list[str] = field(default_factory=list)
    requires_sub_agents: bool = False
    requires_approval: bool = False
    estimated_steps: int = 2
    success_criteria: str = "The task goal is completed with verifiable output."


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_VALID_ACTIONS = {"think", "tool_call", "spawn_sub_agent", "approval_gate", "escalate"}
_TEMPLATE_REF_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


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
    if isinstance(raw.get("gates"), list):
        step["gates"] = [str(gate) for gate in raw["gates"]]
    if raw.get("checkpoint"):
        step["checkpoint"] = str(raw["checkpoint"])
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


def default_available_tools() -> list[str]:
    return [_canonical_tool_name(tool_name(schema)) for schema in ALL_TOOLS]


def validate_plan(raw_plan: Any, available_tools: list[str] | None = None) -> ValidationResult:
    """Validate a DAG plan against the runtime tool surface before execution."""
    errors: list[str] = []
    warnings: list[str] = []
    available = _available_tool_set(available_tools)
    try:
        plan = normalize_plan(raw_plan)
    except PlanningError as exc:
        return ValidationResult(valid=False, errors=[str(exc)])

    output_names: set[str] = set()
    approval_gates = [step for step in plan["steps"] if step["action"] == "approval_gate"]

    for step in plan["steps"]:
        step_id = step["id"]
        action = step["action"]
        tool = _canonical_tool_name(step.get("tool"))
        if action in {"tool_call", "approval_gate"} and tool and tool not in available:
            errors.append(f"Step {step_id} uses unavailable tool: {tool}")

        if action == "tool_call" and _requires_approval(tool):
            if not _has_approval_gate_for_step(step, approval_gates):
                errors.append(f"Step {step_id} tool {tool} requires an approval_gate before execution")

        for ref in _extract_template_roots(step.get("args")):
            if ref not in output_names:
                warnings.append(f"Step {step_id} references {ref}, but no prior output_key or step id provides it")

        output_names.add(step_id)
        if step.get("output_key"):
            output_names.add(str(step["output_key"]))

    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


class TaskPlanner:
    """Goal classifier plus validated DAG planner for explicit execution paths."""

    def __init__(self, available_tools: list[str] | None = None) -> None:
        self.available_tools = default_available_tools() if available_tools is None else available_tools

    async def classify(self, goal: str, context: dict[str, Any] | None = None, org_id: str = "default") -> TaskClassification:
        if _is_operator_workflow_proof(goal):
            return TaskClassification(
                complexity="complex",
                requires_tools=["browser.search", "gmail.draft"],
                requires_sub_agents=False,
                requires_approval=True,
                estimated_steps=3,
                success_criteria="Reach the approval gate with deterministic outreach drafts.",
            )

        prompt = f"""
Classify this task goal before planning.
Return only JSON with:
complexity (simple|medium|complex), requires_tools (array of dot tool names),
requires_sub_agents (boolean), requires_approval (boolean), estimated_steps (integer),
success_criteria (string).

Organization: {org_id}
Context JSON: {json.dumps(context or {}, default=str)}
Goal: {goal}
"""
        try:
            parsed = json.loads(await complete_json(prompt, model=settings.fast_model))
            classification = _classification_from_json(parsed)
            if classification:
                return classification
        except Exception:
            pass
        return _heuristic_classification(goal)

    async def plan(
        self,
        goal: str,
        context: dict[str, Any] | None = None,
        org_id: str = "default",
    ) -> dict[str, Any]:
        classification = await self.classify(goal, context, org_id)
        missing = _missing_tools(classification.requires_tools, self.available_tools)

        if _is_operator_workflow_proof(goal):
            plan = normalize_plan(_operator_workflow_proof_plan(goal))
            plan["context"]["allow_replan"] = False
            return plan

        prompt = f"""
You are Chronos, an enterprise autonomous task planner.
Return only JSON with a top-level "steps" array and optional "context" object.
The steps form a directed acyclic graph. Each step must have:
id, action (think|tool_call|spawn_sub_agent|approval_gate), description, tool, args,
approval_required, depends_on.
Use only tools from this available list: {json.dumps(sorted(_available_tool_set(self.available_tools)))}
Use parallel_group for independent steps, output_key for shared state, and condition for branches.
Insert approval_gate steps for actions that require human approval.

Organization: {org_id}
Task classification: {json.dumps(classification.__dict__, default=str)}
Context JSON: {json.dumps(context or {}, default=str)}
Goal: {goal}
"""
        try:
            parsed = json.loads(await complete_json(prompt, model=settings.agent_model))
            plan = normalize_plan(parsed)
        except Exception:
            fallback = _demo_plan(goal) if classification.requires_approval or _is_outreach_goal(goal) else _research_plan(goal)
            plan = normalize_plan(fallback)
            plan["context"]["allow_replan"] = False
        if missing:
            plan.setdefault("context", {})["classification_tool_hints_unavailable"] = missing

        validation = validate_plan(plan, self.available_tools)
        if not validation.valid:
            raise PlanningError(f"Invalid plan: {'; '.join(validation.errors)}")
        if validation.warnings:
            plan.setdefault("context", {})["validation_warnings"] = validation.warnings
        return plan


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


def _canonical_tool_name(name: Any) -> str:
    if not name:
        return ""
    return to_broker_name(str(name).strip())


def _available_tool_set(available_tools: list[str] | None) -> set[str]:
    tools = default_available_tools() if available_tools is None else available_tools
    canonical = {_canonical_tool_name(tool) for tool in tools if tool}
    return {tool for tool in canonical if tool}


def _missing_tools(required: list[str], available_tools: list[str]) -> list[str]:
    available = _available_tool_set(available_tools)
    missing = []
    for tool in required:
        canonical = _canonical_tool_name(tool)
        if canonical and canonical not in available:
            missing.append(canonical)
    return missing


def _requires_approval(tool: str) -> bool:
    if not tool:
        return False
    approval_tools = {_canonical_tool_name(name) for name in ALWAYS_APPROVAL_TOOL_NAMES}
    return tool in approval_tools


def _has_approval_gate_for_step(
    step: dict[str, Any],
    approval_gates: list[dict[str, Any]],
) -> bool:
    step_id = step["id"]
    depends_on = set(step.get("depends_on") or [])
    for gate in approval_gates:
        if gate["id"] not in depends_on:
            continue
        gates = gate.get("gates")
        if isinstance(gates, list) and step_id in {str(item) for item in gates}:
            return True
        if not gates:
            return True
    return False


def _extract_template_roots(value: Any) -> set[str]:
    roots: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            roots.update(_extract_template_roots(item))
        return roots
    if isinstance(value, list):
        for item in value:
            roots.update(_extract_template_roots(item))
        return roots
    if not isinstance(value, str):
        return roots
    for match in _TEMPLATE_REF_RE.finditer(value):
        path = match.group(1).strip()
        if path:
            root = re.split(r"[.\[]", path, maxsplit=1)[0].strip()
            if root:
                roots.add(root)
    return roots


def _classification_from_json(parsed: Any) -> TaskClassification | None:
    if not isinstance(parsed, dict):
        return None
    if "complexity" not in parsed and "requires_tools" not in parsed:
        return None
    tools = parsed.get("requires_tools")
    return TaskClassification(
        complexity=str(parsed.get("complexity") or "medium").lower(),
        requires_tools=[_canonical_tool_name(tool) for tool in tools if tool] if isinstance(tools, list) else [],
        requires_sub_agents=bool(parsed.get("requires_sub_agents")),
        requires_approval=bool(parsed.get("requires_approval")),
        estimated_steps=max(1, int(parsed.get("estimated_steps") or 2)),
        success_criteria=str(parsed.get("success_criteria") or "The task goal is completed with verifiable output."),
    )


def _heuristic_classification(goal: str) -> TaskClassification:
    normalized = goal.lower()
    tools: list[str] = []
    requires_approval = False
    requires_sub_agents = False
    complexity = "medium"
    estimated_steps = 2

    if any(term in normalized for term in ("latest", "current", "news", "research", "brief", "compare", "market")):
        tools.append("browser.search")
        estimated_steps = max(estimated_steps, 2)
    if any(term in normalized for term in ("lead", "leads", "outreach", "draft", "email", "gmail", "sdr")):
        tools.extend(["browser.search", "gmail.draft"])
        requires_approval = True
        requires_sub_agents = True
        complexity = "complex"
        estimated_steps = max(estimated_steps, 4)
    if any(term in normalized for term in ("file", "write", "save")):
        tools.append("fs.write")
    if any(term in normalized for term in ("calculate", "compute", "python", "csv", "spreadsheet")):
        tools.append("code.python")

    unique_tools = []
    for tool in tools:
        canonical = _canonical_tool_name(tool)
        if canonical and canonical not in unique_tools:
            unique_tools.append(canonical)
    return TaskClassification(
        complexity=complexity,
        requires_tools=unique_tools,
        requires_sub_agents=requires_sub_agents,
        requires_approval=requires_approval,
        estimated_steps=estimated_steps,
        success_criteria="Return a complete answer with citations, artifacts, or approval-ready outputs as applicable.",
    )


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


async def preflight(
    goal: str,
    context: dict[str, Any] | None,
    org_id: str,
    available_tools: list[str] | None = None,
) -> tuple[TaskClassification, list[str]]:
    """Category-4 pre-flight: classify a goal and report required-but-unavailable tools.

    The classification is what actually drives live routing — complex goals are sent
    to the DAG planner, everything else uses the native loop. The returned ``missing``
    list is registry-level only: it fires when the classifier names a tool absent from
    ALL_TOOLS (a hallucinated tool), not when a connector is merely unconnected.
    Connector-aware gating is a follow-up (Capability Roadmap categories 3/4).
    """
    planner = TaskPlanner(available_tools)
    classification = await planner.classify(goal, context, org_id)
    missing = _missing_tools(classification.requires_tools, planner.available_tools)
    return classification, missing


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

    return await TaskPlanner().plan(goal, context, org_id)
