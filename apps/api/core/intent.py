"""Intent classification for chat messages.

Classifies whether a message is a conversational reply or a goal-directed task.
"""
from __future__ import annotations

import re


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


async def classify_intent(message: str) -> dict:
    """Classify a message as 'task' or 'chat'.

    Returns a dict with:
    - ``mode``: "task" or "chat"
    - ``goal``: extracted goal string if mode=="task", else None

    This is a fast, heuristic-only classification — no LLM call.
    """
    text = message.strip()
    low = text.lower()

    for pattern in _GOAL_PATTERNS:
        m = pattern.match(text)
        if m:
            goal = m.group(1).strip()
            return {"mode": "task", "goal": goal}

    if any(verb in low for verb in _TASK_VERBS):
        return {"mode": "task", "goal": text}

    return {"mode": "chat", "goal": None}
