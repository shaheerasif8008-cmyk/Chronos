"""Intent classification for chat messages.

Classifies whether a message is a conversational reply or a goal-directed task.
Uses a fast LLM classification (via ``complete_json``) with a deterministic
heuristic fallback when the model is unavailable or returns something unusable —
mirroring the fallback pattern used elsewhere in ``core.llm``.
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

_CLASSIFY_PROMPT = """Classify the user's message into one of two modes for an \
AI work assistant:
- "task": a goal-directed request that requires doing work — research, drafting, \
sending, multi-step actions, or anything the assistant should execute.
- "chat": a conversational message or a simple question that can be answered \
directly without doing work.

Return ONLY JSON with exactly these keys:
{{"mode": "task" | "chat", "goal": "<a concise display title if mode is task, else null>"}}

The "goal" field is UI metadata only. It must not be treated as the user's
executable instruction and may not replace the original message.

Message:
{message}
"""


def _heuristic_classify(text: str) -> dict:
    """Deterministic fallback classifier — no LLM call."""
    low = text.lower()
    for pattern in _GOAL_PATTERNS:
        m = pattern.match(text)
        if m:
            return {"mode": "task", "goal": m.group(1).strip()}
    if any(verb in low for verb in _TASK_VERBS):
        return {"mode": "task", "goal": text}
    return {"mode": "chat", "goal": None}


async def classify_intent(message: str) -> dict:
    """Classify a message as 'task' or 'chat'.

    Returns a dict with:
    - ``mode``: "task" or "chat"
    - ``goal``: concise display title if mode=="task", else None

    Tries an LLM classification first; on any error or malformed output, falls
    back to a deterministic heuristic so routing always succeeds.
    """
    text = message.strip()
    if not text:
        return {"mode": "chat", "goal": None}

    try:
        raw = await complete_json(_CLASSIFY_PROMPT.format(message=text[:2000]))
        data = json.loads(raw)
        mode = data.get("mode")
        if mode not in {"task", "chat"}:
            raise ValueError(f"unusable mode: {mode!r}")
        goal_raw = data.get("goal")
        goal = str(goal_raw).strip() if (mode == "task" and goal_raw) else None
        return {"mode": mode, "goal": goal}
    except Exception:
        return _heuristic_classify(text)
