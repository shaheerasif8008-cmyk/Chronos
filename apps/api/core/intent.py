"""Intent classification for chat messages.

Classifies whether a message is a conversational reply or a goal-directed task,
and — in the richer ``classify_request`` — how hard the request is, so the chat
turn can adapt how much the model thinks (``reasoning_effort``). Uses a fast LLM
classification (via ``complete_json``) with a deterministic heuristic fallback
when the model is unavailable or returns something unusable — mirroring the
fallback pattern used elsewhere in ``core.llm``.
"""
from __future__ import annotations

import json
import re

from core.llm import complete_json


_TASK_VERBS = (
    "research", "find", "search", "look up", "look for", "draft", "write",
    "send", "email", "schedule", "create", "build", "generate", "analyze",
    "summarize", "compare", "fetch", "browse", "book", "order", "set up",
    "configure", "organize", "plan", "prepare", "compile",
)

_GOAL_PATTERNS = [
    re.compile(r"^(?:can you|could you|please|i need you to|i want you to)\s+(.+)", re.I),
    re.compile(r"^(?:go ahead and|go and)\s+(.+)", re.I),
]

# Difficulty → reasoning effort. Higher difficulty makes the model think harder.
# "trivial" maps to None (no reasoning overhead) — greetings, acks, one-liners.
_DIFFICULTY_LEVELS = ("trivial", "simple", "standard", "hard")
_EFFORT_BY_DIFFICULTY: dict[str, str | None] = {
    "trivial": None,
    "simple": "low",
    "standard": "medium",
    "hard": "high",
}


def effort_for_difficulty(difficulty: str | None) -> str | None:
    """Recommended reasoning_effort for a classified difficulty (None if unknown)."""
    return _EFFORT_BY_DIFFICULTY.get((difficulty or "").strip().lower())


_CLASSIFY_PROMPT = """Classify the user's message for an AI work assistant.

Decide two things:

1. mode:
   - "task": a goal-directed request that requires doing work — research, \
drafting, sending, multi-step actions, or anything the assistant should execute.
   - "chat": a conversational message or a question answered directly without \
doing work.

2. difficulty — how much careful reasoning a *high-quality* answer needs:
   - "trivial": greetings, acknowledgements, one-word replies, trivial lookups.
   - "simple": a single clear fact or a short direct answer; little reasoning.
   - "standard": needs explanation, structure, comparison, or a few steps of \
thought; most substantive questions and ordinary tasks.
   - "hard": deep analysis, multi-step reasoning, tradeoffs, planning, synthesis \
across sources, nuanced judgement, or anything where a shallow answer would be \
visibly worse.

When unsure between two difficulties, choose the higher one — a thoughtful answer
is better than a thin one.

Return ONLY JSON with exactly these keys:
{{"mode": "task" | "chat", "difficulty": "trivial" | "simple" | "standard" | \
"hard", "goal": "<a concise display title if mode is task, else null>"}}

The "goal" field is UI metadata only. It must not be treated as the user's
executable instruction and may not replace the original message.

Message:
{message}
"""


def _heuristic_difficulty(text: str, mode: str) -> str:
    """Deterministic difficulty estimate — no LLM call."""
    low = text.lower()
    words = len(text.split())
    deep_markers = (
        "analyze", "analyse", "compare", "evaluate", "strategy", "tradeoff",
        "trade-off", "why", "design", "architecture", "plan", "research",
        "pros and cons", "recommend", "should i", "explain how", "implications",
    )
    if any(marker in low for marker in deep_markers) or words >= 40:
        return "hard"
    if mode == "task" or words >= 12:
        return "standard"
    if words <= 4:
        return "trivial"
    return "simple"


def _heuristic_classify(text: str) -> dict:
    """Deterministic fallback classifier — no LLM call."""
    low = text.lower()
    for pattern in _GOAL_PATTERNS:
        m = pattern.match(text)
        if m:
            return {
                "mode": "task",
                "goal": m.group(1).strip(),
                "difficulty": _heuristic_difficulty(text, "task"),
            }
    if any(verb in low for verb in _TASK_VERBS):
        return {"mode": "task", "goal": text, "difficulty": _heuristic_difficulty(text, "task")}
    return {"mode": "chat", "goal": None, "difficulty": _heuristic_difficulty(text, "chat")}


async def classify_request(message: str) -> dict:
    """Model-driven request router.

    Returns a dict with:
    - ``mode``: "task" or "chat"
    - ``goal``: concise display title if mode=="task", else None
    - ``difficulty``: "trivial" | "simple" | "standard" | "hard"
    - ``reasoning_effort``: recommended effort for this turn, or None

    A single fast-model call classifies both mode and difficulty; difficulty
    drives how hard the model should think on the real turn. On any error or
    malformed output, falls back to a deterministic heuristic so routing always
    succeeds (and never blocks the turn).
    """
    text = message.strip()
    if not text:
        return {"mode": "chat", "goal": None, "difficulty": "trivial", "reasoning_effort": None}

    try:
        raw = await complete_json(_CLASSIFY_PROMPT.format(message=text[:2000]))
        data = json.loads(raw)
        mode = data.get("mode")
        if mode not in {"task", "chat"}:
            raise ValueError(f"unusable mode: {mode!r}")
        goal_raw = data.get("goal")
        goal = str(goal_raw).strip() if (mode == "task" and goal_raw) else None
        difficulty = str(data.get("difficulty") or "").strip().lower()
        if difficulty not in _DIFFICULTY_LEVELS:
            difficulty = _heuristic_difficulty(text, mode)
        result = {"mode": mode, "goal": goal, "difficulty": difficulty}
    except Exception:
        result = _heuristic_classify(text)

    result["reasoning_effort"] = effort_for_difficulty(result.get("difficulty"))
    return result


async def classify_intent(message: str) -> dict:
    """Classify a message as 'task' or 'chat'.

    Back-compat shape ({"mode", "goal"}) for callers that only route on intent.
    Delegates to ``classify_request`` and drops the difficulty/effort fields.
    """
    result = await classify_request(message)
    return {"mode": result["mode"], "goal": result.get("goal")}
