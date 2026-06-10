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


# ── Plan ─────────────────────────────────────────────────────────────────────

# The plan is persisted in the ``tasks.plan`` JSONB column in the shape the web
# UI already renders: ``{"steps": [{"id", "action", "description", "tool"}]}``.

_PLAN_PROMPT = """You are the planner for an autonomous AI work assistant.

Break the user's goal into 2-6 concrete, ordered steps a tool-using agent will \
execute. Each step is one meaningful unit of work (research, analyze, draft, \
review, etc.) — not a single tool call, and not low-level mechanics.

Return ONLY JSON:
{{"steps": [{{"description": "<short imperative step>", "tool_hint": "<optional tool family or null>"}}]}}

Keep it tight. If the goal is simple enough to answer in one step, return a \
single step. Do not include sign-off or "respond to user" steps.

Goal:
{goal}
"""


def build_plan_prompt(goal: str) -> str:
    return _PLAN_PROMPT.format(goal=goal[:2000])


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
