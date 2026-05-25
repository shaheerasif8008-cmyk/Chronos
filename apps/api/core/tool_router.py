from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from core.config import settings
from core.llm import complete_json


@dataclass
class ToolRoutingDecision:
    tool: str | None
    confidence: float
    reasoning: str
    fallback_tool: str | None = None


def _tool_available(tool: str | None, available_tools: list[str]) -> str | None:
    if tool and tool in available_tools:
        return tool
    return None


def _heuristic_route(message: str, available_tools: list[str]) -> ToolRoutingDecision:
    normalized = " ".join(message.lower().split())
    has = set(available_tools)
    if any(term in normalized for term in ("latest", "current", "recent", "today", "news", "this week")):
        tool = _tool_available("browser__search", available_tools)
        if tool:
            return ToolRoutingDecision(tool=tool, confidence=0.86, reasoning="Time-sensitive request needs live search.")
    if re.search(r"https?://", message):
        tool = _tool_available("browser__fetch", available_tools)
        if tool:
            return ToolRoutingDecision(tool=tool, confidence=0.82, reasoning="URL provided; fetch the page.")
    if any(term in normalized for term in ("write file", "save file", "create file")):
        tool = _tool_available("fs__write", available_tools)
        if tool:
            return ToolRoutingDecision(tool=tool, confidence=0.78, reasoning="Request asks to create a workspace file.")
    if any(term in normalized for term in ("calculate", "analyze csv", "python", "compute")):
        tool = _tool_available("code__python", available_tools)
        if tool:
            return ToolRoutingDecision(tool=tool, confidence=0.78, reasoning="Computation request fits code execution.")
    fallback = "browser__search" if "browser__search" in has else None
    return ToolRoutingDecision(tool=None, confidence=0.0, reasoning="No deterministic route matched.", fallback_tool=fallback)


async def route(message: str, available_tools: list[str]) -> ToolRoutingDecision:
    if not available_tools:
        return ToolRoutingDecision(tool=None, confidence=0.0, reasoning="No tools available.")
    prompt = f"""
Given this user or task message, choose the single best first tool if one is clearly needed.
Only choose from the available tools. If no tool is clearly needed, return null.

Available tools: {json.dumps(available_tools)}
Message: {message}

Return JSON only:
{{"tool":"tool_name or null","confidence":0.0,"reasoning":"one sentence","fallback_tool":"tool_name or null"}}
"""
    try:
        parsed = json.loads(await complete_json(prompt, model=settings.fast_model))
        tool = _tool_available(parsed.get("tool"), available_tools)
        fallback = _tool_available(parsed.get("fallback_tool"), available_tools)
        confidence = max(0.0, min(float(parsed.get("confidence") or 0.0), 1.0))
        if confidence < 0.6 and fallback:
            tool = fallback
            confidence = 0.6
        if tool:
            return ToolRoutingDecision(
                tool=tool,
                confidence=confidence,
                reasoning=str(parsed.get("reasoning") or "Model-selected route."),
                fallback_tool=fallback,
            )
    except Exception:
        pass
    return _heuristic_route(message, available_tools)
