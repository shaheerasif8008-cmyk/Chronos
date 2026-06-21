"""Cognitive layer for the agent loop — plan / progress / budget / reflection.

These components turn the model-native tool loop into an explicit
plan-execute-reflect controller. Every piece degrades safely: the planner and
critic return neutral defaults on any failure or malformed output, and the
budget/progress trackers are pure logic with no model dependency. When a model
is unavailable the loop therefore behaves exactly like the prior model-native
loop — the cognition is strictly additive.

Pure builders/parsers live here so they can be unit-tested without a network;
the thin LLM-invoking wrappers live in ``agent_loop`` so they can be
monkeypatched in tests alongside ``_llm_step``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ── Budget ──────────────────────────────────────────────────────────────────


@dataclass
class LoopBudget:
    """Tracks the step budget and surfaces it to the model so it can triage.

    ``max_iterations`` is the hard cap (same value the loop has always used);
    ``directive`` returns a short, escalating nudge as the remaining budget
    shrinks so the model stops exploring and converges in time.
    """

    max_iterations: int
    used: int = 0
    max_reflections: int = 2
    reflections_used: int = 0
    max_replans: int = 3
    replans_used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_iterations - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_iterations

    @property
    def can_reflect(self) -> bool:
        return self.reflections_used < self.max_reflections

    @property
    def can_replan(self) -> bool:
        return self.replans_used < self.max_replans

    def directive(self) -> str | None:
        """Plain-language budget guidance, or None when there's plenty left."""
        remaining = self.remaining
        if remaining <= 0:
            return (
                "Budget: no tool-steps remain. Deliver the best possible final answer now "
                "from the evidence already gathered."
            )
        if remaining <= 3:
            return (
                f"Budget: only {remaining} tool-step(s) left. Stop exploring, finish the most "
                "important remaining work, and prepare your final answer."
            )
        soft = max(6, self.max_iterations // 3)
        if remaining <= soft:
            return (
                f"Budget: about {remaining} tool-step(s) left. Prioritize the highest-value "
                "remaining actions and avoid low-value detours."
            )
        return None


# ── Progress / loop detection ───────────────────────────────────────────────


class ProgressSignal(str, Enum):
    OK = "ok"
    REPEAT_CALL = "repeat_call"  # identical tool+args seen too often
    REPEAT_ERROR = "repeat_error"  # identical error recurring
    STALLED = "stalled"  # a run of iterations with no productive result


def call_signature(name: str, args: dict[str, Any]) -> str:
    """Stable signature for a tool call, used to spot exact repeats."""
    try:
        blob = json.dumps(args or {}, sort_keys=True, default=str)
    except Exception:
        blob = str(args)
    return f"{name}:{hashlib.sha256(blob.encode()).hexdigest()[:16]}"


def _message_is_error(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, str):
        return False
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(data, dict) and bool(data.get("error"))


def _error_signature(message: dict[str, Any]) -> str:
    name = str(message.get("name") or "")
    content = message.get("content") or ""
    try:
        err = str(json.loads(content).get("error"))
    except Exception:
        err = str(content)
    return f"{name}:{hashlib.sha256(err.encode()).hexdigest()[:12]}"


@dataclass
class ProgressTracker:
    """Detects non-progress: identical repeated calls, recurring errors, stalls.

    The loop feeds each iteration's calls and resulting tool messages in; the
    tracker returns a :class:`ProgressSignal` and an optional controller
    directive that nudges the model to change strategy. After enough
    unproductive breaks it flags ``should_force_finish`` so the loop wraps up
    gracefully instead of burning the full iteration budget.
    """

    repeat_threshold: int = 2
    stall_window: int = 4
    _call_counts: dict[str, int] = field(default_factory=dict)
    _error_counts: dict[str, int] = field(default_factory=dict)
    unproductive_streak: int = 0
    stall_breaks: int = 0

    def record(
        self,
        calls: list[dict[str, Any]],
        tool_messages: list[dict[str, Any]],
        *,
        args_for: Any = None,
    ) -> ProgressSignal:
        """Record one iteration and return its progress signal.

        ``args_for`` optionally maps a call to its parsed args dict; when absent
        the call's own ``args``/``args_str`` is used.
        """
        repeated_call = False
        for call in calls:
            args = {}
            if args_for is not None:
                try:
                    args = args_for(call) or {}
                except Exception:
                    args = {}
            elif isinstance(call.get("args"), dict):
                args = call["args"]
            sig = call_signature(str(call.get("name") or ""), args)
            self._call_counts[sig] = self._call_counts.get(sig, 0) + 1
            if self._call_counts[sig] >= self.repeat_threshold + 1:
                repeated_call = True

        repeated_error = False
        errors = 0
        for message in tool_messages:
            if not _message_is_error(message):
                continue
            errors += 1
            sig = _error_signature(message)
            self._error_counts[sig] = self._error_counts.get(sig, 0) + 1
            if self._error_counts[sig] >= self.repeat_threshold + 1:
                repeated_error = True

        productive = bool(tool_messages) and errors < len(tool_messages)
        self.unproductive_streak = 0 if productive else self.unproductive_streak + 1

        if repeated_error:
            return ProgressSignal.REPEAT_ERROR
        if repeated_call:
            return ProgressSignal.REPEAT_CALL
        if self.unproductive_streak >= self.stall_window:
            return ProgressSignal.STALLED
        return ProgressSignal.OK

    def directive(self, signal: ProgressSignal) -> str | None:
        """Controller nudge for a non-OK signal (and count it as a break)."""
        if signal == ProgressSignal.OK:
            return None
        self.stall_breaks += 1
        if signal == ProgressSignal.REPEAT_CALL:
            return (
                "Controller observation: you are repeating the same tool call with the same "
                "arguments without making progress. Change strategy — use a different tool, "
                "different arguments, or synthesize an answer from what you already have. Do not "
                "repeat that exact call again."
            )
        if signal == ProgressSignal.REPEAT_ERROR:
            return (
                "Controller observation: the same error keeps recurring. Stop retrying it. Either "
                "work around it with a different approach, or finalize honestly and state what "
                "could not be completed."
            )
        return (
            "Controller observation: several recent steps produced no useful new information. "
            "Reassess the goal, switch approach, or deliver the best answer possible from the "
            "evidence gathered so far."
        )

    @property
    def should_force_finish(self) -> bool:
        """True once the loop should be made to wrap up rather than keep grinding."""
        return self.stall_breaks >= 3 or self.unproductive_streak >= self.stall_window * 2


# ── Controller ledger / plan compliance / trace scoring ─────────────────────


class PlanComplianceSignal(str, Enum):
    NO_PLAN = "no_plan"
    ON_PLAN = "on_plan"
    TOOL_MISMATCH = "tool_mismatch"
    STEP_BLOCKED = "step_blocked"


@dataclass
class PlanComplianceReport:
    signal: PlanComplianceSignal
    directive: str = ""
    should_advance: bool = False


def _tool_family(name: str | None) -> str:
    raw = str(name or "")
    return raw.partition("__")[0] or raw.partition(".")[0]


def _call_name(call: dict[str, Any]) -> str:
    if call.get("name"):
        return str(call["name"])
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(fn.get("name") or "")


def _parse_tool_payload(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if not isinstance(content, str):
        return {}
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {"summary": content[:240]}
    return payload if isinstance(payload, dict) else {"summary": str(payload)[:240]}


def _tool_summary(message: dict[str, Any]) -> str:
    name = str(message.get("name") or "tool")
    payload = _parse_tool_payload(message)
    parts = [f"- {name}"]
    if payload.get("summary"):
        parts.append(f"summary={payload['summary']}")
    if payload.get("error"):
        parts.append(f"error={payload['error']}")
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("artifact_id", "artifact_ids", "source_id", "source_ids", "citations", "degraded_connector"):
            if key in data:
                parts.append(f"{key}={str(data[key])[:240]}")
    return "; ".join(parts)


def _artifact_ids_from_payload(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return out
    single = data.get("artifact_id")
    if isinstance(single, str) and single:
        out.append(single)
    for artifact_id in data.get("artifact_ids") or []:
        if isinstance(artifact_id, str) and artifact_id:
            out.append(artifact_id)
    return out


def assess_plan_compliance(
    steps: list[dict[str, Any]] | None,
    *,
    current_step: int,
    calls: list[dict[str, Any]],
    tool_messages: list[dict[str, Any]],
) -> PlanComplianceReport:
    """Return a compact controller assessment of whether the current step is on track."""
    if not steps:
        return PlanComplianceReport(PlanComplianceSignal.NO_PLAN)
    step = steps[min(max(current_step, 0), len(steps) - 1)]
    hinted_family = _tool_family(str(step.get("tool") or ""))
    call_families = {_tool_family(_call_name(call)) for call in calls if _call_name(call)}
    errors = [msg for msg in tool_messages if _message_is_error(msg)]
    if errors:
        return PlanComplianceReport(
            PlanComplianceSignal.STEP_BLOCKED,
            (
                "Controller plan check: the current plan step is blocked by tool errors; "
                "work around the failure with a different tool, narrower arguments, or a "
                "truthful partial result instead of retrying blindly."
            ),
        )
    if hinted_family and call_families and hinted_family not in call_families:
        return PlanComplianceReport(
            PlanComplianceSignal.TOOL_MISMATCH,
            (
                "Controller plan check: the current step expected the "
                f"{hinted_family} tool family, but the model chose {', '.join(sorted(call_families))}. "
                "Either explain why the plan changed in the next action or return to the planned step."
            ),
        )
    productive = bool(tool_messages)
    return PlanComplianceReport(PlanComplianceSignal.ON_PLAN, should_advance=productive)


def build_task_ledger(
    goal: str,
    history: list[dict[str, Any]],
    *,
    plan_steps: list[dict[str, Any]] | None = None,
    current_plan_step: int = 0,
    token_usage: dict[str, Any] | None = None,
    pending_approval_calls: list[dict[str, Any]] | None = None,
    max_recent_evidence: int = 8,
) -> dict[str, Any]:
    """Build the small state object the controller shows the model each turn."""
    tool_messages = [m for m in history if m.get("role") == "tool"]
    attempted: list[str] = []
    errors: list[dict[str, str]] = []
    artifacts: list[str] = []
    for message in tool_messages:
        name = str(message.get("name") or "tool")
        if name not in attempted:
            attempted.append(name)
        payload = _parse_tool_payload(message)
        if payload.get("error"):
            errors.append({"tool": name, "error": str(payload["error"])[:240]})
        artifacts.extend(_artifact_ids_from_payload(payload))
    plan: dict[str, Any] = {}
    if plan_steps:
        index = min(max(current_plan_step, 0), len(plan_steps) - 1)
        plan = {
            "current_step_index": index,
            "current_step": plan_steps[index],
            "step_count": len(plan_steps),
        }
    return {
        "goal": goal[:500],
        "plan": plan,
        "attempted_tools": attempted,
        "recent_evidence": [_tool_summary(m) for m in tool_messages[-max_recent_evidence:]],
        "open_errors": errors[-5:],
        "artifacts": list(dict.fromkeys(artifacts))[-10:],
        "pending_approval_count": len(pending_approval_calls or []),
        "token_usage": {
            "total_tokens": int((token_usage or {}).get("total_tokens") or 0),
            "steps": len((token_usage or {}).get("steps") or []),
        },
    }


def render_task_ledger(ledger: dict[str, Any]) -> str:
    """Render the structured ledger into a terse system message."""
    lines = ["# Controller Task Ledger"]
    goal = ledger.get("goal")
    if goal:
        lines.append(f"Goal: {goal}")
    plan = ledger.get("plan") if isinstance(ledger.get("plan"), dict) else {}
    if plan:
        current = plan.get("current_step") or {}
        lines.append(
            f"Plan: step {int(plan.get('current_step_index') or 0) + 1}/{plan.get('step_count')} - "
            f"{str(current.get('description') or '')[:200]}"
        )
    attempted = ledger.get("attempted_tools") or []
    if attempted:
        lines.append("Tools attempted: " + ", ".join(str(t) for t in attempted[-12:]))
    if ledger.get("pending_approval_count"):
        lines.append(f"Pending approvals: {ledger['pending_approval_count']}")
    if ledger.get("artifacts"):
        lines.append("Artifacts: " + ", ".join(str(a) for a in ledger["artifacts"]))
    evidence = ledger.get("recent_evidence") or []
    if evidence:
        lines.append("Recent evidence:")
        lines.extend(str(item)[:500] for item in evidence)
    errors = ledger.get("open_errors") or []
    if errors:
        lines.append("Open errors:")
        for error in errors:
            lines.append(f"- {error.get('tool')}: {error.get('error')}")
    usage = ledger.get("token_usage") if isinstance(ledger.get("token_usage"), dict) else {}
    if usage.get("total_tokens"):
        lines.append(f"Observed tokens: {usage['total_tokens']} across {usage.get('steps', 0)} model steps")
    return "\n".join(lines)


def build_evidence_verification_prompt(
    goal: str,
    answer: str,
    ledger: dict[str, Any],
    tool_summaries: list[str],
) -> str:
    """Prompt a critic to judge whether the final answer is backed by runtime evidence."""
    return (
        "You are the evidence verifier for an autonomous AI work assistant. Judge whether the "
        "proposed final answer is supported by the actual task ledger and tool evidence. "
        "Do not demand unnecessary work, but reject answers that claim files, research, actions, "
        "or external changes that are not visible in the evidence.\n\n"
        "Return ONLY JSON:\n"
        '{"complete": true|false, "missing": ["<gap>", ...], '
        '"directive": "<one concrete instruction for what to do next, or empty if complete>"}\n\n'
        f"Goal:\n{goal[:1500]}\n\n"
        f"Controller ledger:\n{json.dumps(ledger, indent=2, default=str)[:5000]}\n\n"
        f"Tool summaries:\n{chr(10).join(f'- {s}' for s in tool_summaries[:30])[:4000]}\n\n"
        f"Proposed final answer:\n{(answer or '')[:4000]}"
    )


def subagent_orchestration_directive(
    goal: str,
    steps: list[dict[str, Any]] | None,
    *,
    available_tool_names: list[str],
) -> str | None:
    if "spawn__subagent" not in available_tool_names:
        return None
    goal_lower = goal.lower()
    broad_goal = any(word in goal_lower for word in ("all ", "compare", "audit", "research", "competitor", "multiple"))
    if broad_goal or len(steps or []) >= 3:
        return (
            "Controller strategy: if the remaining work has independent workstreams, spawn the useful "
            "parallel sub-agents in one assistant step with distinct roles, evidence requirements, and "
            "small budgets. Synthesize their reports instead of serializing the same research yourself."
        )
    return None


def tool_batching_directive(available_tool_names: list[str]) -> str | None:
    names = set(available_tool_names)
    if "code__python" not in names:
        return None
    if len(names) < 4:
        return None
    return (
        "Controller strategy: use code__python to batch local data processing, filtering, joins, and "
        "artifact generation. Keep network and connector access in broker tools, but do not route large "
        "intermediate tables or repetitive transformations through the model when code can summarize them."
    )


def score_task_trace(history: list[dict[str, Any]], result: dict[str, Any] | None = None) -> dict[str, Any]:
    tool_calls = 0
    tool_errors = 0
    tool_results = 0
    assistant_finals = 0
    seen_call_names: dict[str, int] = {}
    for message in history:
        if message.get("tool_calls"):
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict):
                    name = _call_name(call)
                    tool_calls += 1
                    seen_call_names[name] = seen_call_names.get(name, 0) + 1
        if message.get("role") == "tool":
            tool_results += 1
            if _message_is_error(message):
                tool_errors += 1
        if message.get("role") == "assistant" and str(message.get("content") or "").strip() and not message.get("tool_calls"):
            assistant_finals += 1
    success_rate = 1.0 if tool_results == 0 else (tool_results - tool_errors) / max(tool_results, 1)
    return {
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "tool_errors": tool_errors,
        "tool_success_rate": round(success_rate, 3),
        "repeated_tool_names": {name: count for name, count in seen_call_names.items() if count > 1},
        "final_answer_present": bool((result or {}).get("answer")) or assistant_finals > 0,
    }


def derive_trace_learning_notes(metrics: dict[str, Any], ledger: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if int(metrics.get("tool_errors") or 0) > 0 or ledger.get("open_errors"):
        notes.append("Add or refine a recovery playbook for the recurring failing tool family.")
    if float(metrics.get("tool_success_rate") or 1.0) < 0.6:
        notes.append("Review connector health and tool routing: this trace had a low tool success rate.")
    repeated = metrics.get("repeated_tool_names") or {}
    if repeated:
        notes.append("Tune progress detection or prompts for repeated tool choices: " + ", ".join(repeated.keys()))
    if metrics.get("final_answer_present") and not ledger.get("recent_evidence"):
        notes.append("Require evidence verification for final answers that claim completed work without tool evidence.")
    return notes


# ── Plan ─────────────────────────────────────────────────────────────────────

# The plan is persisted in the ``tasks.plan`` JSONB column in the shape the web
# UI already renders: ``{"steps": [{"id", "action", "description", "tool"}]}``.

_PLAN_PROMPT = """You are the planner for an autonomous AI work assistant.

Break the user's goal into 2-6 concrete, ordered steps a tool-using agent will \
execute. Each step is one meaningful unit of work (research, analyze, draft, \
review, etc.) — not a single tool call, and not low-level mechanics.

Plan only for what the available tools can actually do — do not invent \
capabilities the agent does not have. If a tool you would need is unavailable or \
degraded (see notes), plan around it and say so in the relevant step.
{tools_block}{availability_block}
Return ONLY JSON:
{{"steps": [{{"description": "<short imperative step>", "tool_hint": "<one of the tool families above, or null>"}}]}}

Keep it tight. If the goal is simple enough to answer in one step, return a \
single step. Do not include sign-off or "respond to user" steps.

Goal:
{goal}
"""


def summarize_tools(tools: list[dict[str, Any]] | None) -> str:
    """Compact 'family: example actions' view of available tools, for the planner.

    Groups OpenAI-format tool schemas by their ``family__action`` name prefix so
    the planner sees what the agent can actually do without the full schemas."""
    if not tools:
        return ""
    families: dict[str, list[str]] = {}
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        name = str((fn or {}).get("name") or "")
        if not name:
            continue
        family, _, action = name.partition("__")
        actions = families.setdefault(family, [])
        if action and action not in actions and len(actions) < 5:
            actions.append(action)
    lines = []
    for family in sorted(families):
        actions = ", ".join(families[family]) or "(actions)"
        lines.append(f"- {family}: {actions}")
    return "\n".join(lines)


def build_plan_prompt(
    goal: str,
    *,
    tools_summary: str | None = None,
    availability_note: str | None = None,
) -> str:
    tools_block = (
        f"\nTools available to the agent (family: example actions):\n{tools_summary}\n"
        if tools_summary
        else ""
    )
    availability_block = (
        f"\nConnector availability notes:\n{availability_note}\n"
        if availability_note
        else ""
    )
    return _PLAN_PROMPT.format(
        goal=goal[:2000], tools_block=tools_block, availability_block=availability_block
    )


def parse_plan(raw: str, *, max_steps: int = 6) -> list[dict[str, Any]] | None:
    """Parse planner JSON into the persisted step shape, or None if unusable."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    steps_raw = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps_raw, list) or not steps_raw:
        return None
    steps: list[dict[str, Any]] = []
    for i, step in enumerate(steps_raw[:max_steps]):
        if not isinstance(step, dict):
            continue
        description = str(step.get("description") or "").strip()
        if not description:
            continue
        tool_hint = step.get("tool_hint")
        steps.append(
            {
                "id": f"step-{i + 1}",
                "action": "think",
                "description": description[:200],
                "tool": str(tool_hint) if tool_hint else None,
            }
        )
    return steps or None


def plan_directive(steps: list[dict[str, Any]], current_step: int) -> str | None:
    """A compact view of the plan and where the agent is in it, for the model."""
    if not steps:
        return None
    lines = ["Plan (revise as you learn; you are not bound to it rigidly):"]
    for i, step in enumerate(steps):
        marker = "→" if i == current_step else ("✓" if i < current_step else "·")
        lines.append(f"  {marker} {i + 1}. {step.get('description', '')}")
    lines.append(
        "Work the current step (→). When it is done, move on. If the plan is wrong, adapt it."
    )
    return "\n".join(lines)


# ── Reflection / self-verify ─────────────────────────────────────────────────

_REFLECT_PROMPT = """You are the critic for an autonomous AI work assistant. The \
agent believes it has finished. Judge whether the answer genuinely and \
completely satisfies the goal.

Be pragmatic, not perfectionist: accept an answer that a reasonable user would \
consider complete and correct. Only reject if something the goal clearly asked \
for is missing, wrong, or unverified.

Return ONLY JSON:
{{"complete": true|false, "missing": ["<gap>", ...], "directive": "<one concrete \
instruction for what to do next, or empty if complete>"}}

Goal:
{goal}

What the agent did (tool result summaries):
{evidence}

The agent's proposed final answer:
{answer}
"""


def build_reflection_prompt(goal: str, answer: str, tool_summaries: list[str]) -> str:
    evidence = "\n".join(f"- {s}" for s in tool_summaries[:20]) or "(no tools were used)"
    return _REFLECT_PROMPT.format(
        goal=goal[:1500], evidence=evidence[:3000], answer=(answer or "")[:4000]
    )


@dataclass
class Verdict:
    complete: bool
    missing: list[str]
    directive: str


def parse_verdict(raw: str) -> Verdict | None:
    """Parse critic JSON, or None if unusable (caller then accepts the answer)."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "complete" not in data:
        return None
    missing = data.get("missing")
    return Verdict(
        complete=bool(data.get("complete")),
        missing=[str(m) for m in missing] if isinstance(missing, list) else [],
        directive=str(data.get("directive") or "").strip(),
    )
