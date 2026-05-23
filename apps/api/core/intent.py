from __future__ import annotations

import json
import re
from typing import Any

from core.config import settings
from core.llm import complete_json


_TASK_VERBS = {
    "analyze",
    "build",
    "compile",
    "create",
    "draft",
    "find",
    "prepare",
    "pull",
    "research",
    "send",
    "summarize",
    "write",
}
_TASK_OBJECTS = {
    "brief",
    "companies",
    "contacts",
    "draft",
    "email",
    "leads",
    "outreach",
    "report",
    "research",
    "workflow",
}
_CHAT_MARKERS = {
    "explain",
    "how does",
    "what is",
    "what are",
    "why",
}


def _heuristic_intent(message: str) -> dict[str, Any]:
    normalized = " ".join(message.lower().split())
    if "operator workflow proof" in normalized:
        return {"mode": "task", "confidence": 1.0, "goal": message}

    tokens = set(re.findall(r"[a-z0-9']+", normalized))
    has_task_verb = bool(tokens & _TASK_VERBS)
    has_task_object = bool(tokens & _TASK_OBJECTS)
    has_multi_step = any(marker in normalized for marker in (" and ", " then ", " for each", " across ", " list of "))
    starts_chatty = any(normalized.startswith(marker) for marker in _CHAT_MARKERS)

    if has_task_verb and (has_task_object or has_multi_step) and not (starts_chatty and not has_multi_step):
        return {"mode": "task", "confidence": 0.72, "goal": message}
    return {"mode": "chat", "confidence": 0.6, "goal": ""}


async def classify_intent(message: str) -> dict[str, Any]:
    """
    Classify before any streaming response. The LLM path improves recall, while
    the heuristic fallback keeps task routing available when no model is ready.
    """
    prompt = f"""
Return only JSON with this shape:
{{"mode":"chat|task","confidence":0.0,"goal":"cleaned task goal or empty"}}

Use "task" when the user is asking Chronos to perform autonomous work across
steps, tools, research, drafting, connector actions, approvals, or workflow
execution. Use "chat" for Q&A, explanation, discussion, or simple advice.

Message:
{message}
"""
    try:
        parsed = json.loads(await complete_json(prompt, model=settings.fast_model))
    except Exception:
        return _heuristic_intent(message)

    mode = parsed.get("mode")
    if mode not in {"chat", "task"}:
        return _heuristic_intent(message)
    confidence = parsed.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.55:
        return _heuristic_intent(message)
    return {
        "mode": mode,
        "confidence": max(0.0, min(confidence, 1.0)),
        "goal": str(parsed.get("goal") or message if mode == "task" else ""),
    }
