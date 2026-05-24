"""Token budgeting + history compaction (Category 7).

Shared by the chat path (`core/context.py`) and the autonomous agent loop
(`runtime/agent_loop.py`) so the two never diverge on how a context window is
measured or trimmed.

Estimation is deliberately cheap and provider-agnostic: ~4 characters per token
for English prose. This is an estimate, not an exact tokenizer — budgets carry a
response reserve so the approximation stays safe.

Compaction for the agent loop is **turn-aware**: it never breaks the pairing
between an assistant `tool_calls` message and its `tool` result messages, which
OpenAI/Anthropic (via litellm) reject. A "turn" is one assistant message plus
every `tool` message that follows it until the next assistant message. The
preamble (system messages + the first user goal) is always preserved, the most
recent turns are kept verbatim, and the older turns are collapsed into a single
summary message.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

# 4 chars ≈ 1 token for English prose (matches the prior context.py heuristic).
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate the token count of a string. Always at least 1 for non-empty text."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def _message_text(message: dict[str, Any]) -> str:
    """Flatten a chat message into the text whose length approximates its tokens.

    Includes tool_call function names + arguments, which can be large (e.g. a
    whole file passed as a tool argument).
    """
    parts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif content is not None:
        parts.append(json.dumps(content, default=str))
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        parts.append(str(fn.get("name") or ""))
        parts.append(str(fn.get("arguments") or ""))
    name = message.get("name")
    if name:
        parts.append(str(name))
    return "\n".join(parts)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate the combined token count of a list of chat messages.

    Adds a small per-message overhead for role/formatting framing.
    """
    return sum(estimate_tokens(_message_text(m)) + 4 for m in messages)


def split_into_turns(body: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group a message list into turns, each beginning at an assistant message.

    A turn is `[assistant, tool, tool, ...]`. Any leading non-assistant messages
    (which should not occur in a well-formed loop, but we stay defensive) form
    the first group so they are never silently orphaned.
    """
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in body:
        if message.get("role") == "assistant" and current:
            turns.append(current)
            current = [message]
        else:
            current.append(message)
    if current:
        turns.append(current)
    return turns


# Hard ceiling on the digest fed to/produced by the summarizer, so the summary
# message itself can never blow the budget regardless of how much history it
# condenses. ~2k tokens.
_MAX_SUMMARY_CHARS = 8_000


def _default_summary(old_turns: list[list[dict[str, Any]]], *, max_chars_per_msg: int = 300) -> str:
    """Build a deterministic textual digest of old turns (fallback, no LLM)."""
    lines: list[str] = []
    for turn in old_turns:
        for message in turn:
            role = str(message.get("role", "")).upper()
            text = _message_text(message).strip().replace("\n", " ")
            if text:
                lines.append(f"{role}: {text[:max_chars_per_msg]}")
    return "\n".join(lines)


def _summary_message(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": f"[Earlier task progress summary]: {text[:_MAX_SUMMARY_CHARS]}"}


async def compact_agent_history(
    history: list[dict[str, Any]],
    *,
    budget_tokens: int,
    keep_recent_turns: int = 4,
    summarizer: Callable[[str], Awaitable[str]] | None = None,
) -> list[dict[str, Any]]:
    """Compact an agent-loop history to fit within `budget_tokens`.

    Preserves tool_call/tool_result pairing by only ever dropping or summarizing
    *whole turns*. The preamble (leading system messages + the first user goal)
    and the most recent turns are kept verbatim; older turns are collapsed into a
    single summary message inserted right after the preamble.

    The number of recent turns kept is reduced (from `keep_recent_turns` down to
    1) until the result fits the budget — so when a fit is achievable, the output
    is guaranteed under budget, which also makes a second call a true no-op.

    Args:
        history: The full message list.
        budget_tokens: Target ceiling for the returned history.
        keep_recent_turns: Preferred number of trailing turns to keep verbatim.
        summarizer: Optional async fn that condenses the digest text. If None (or
            it raises), a deterministic truncated digest is used. Either way the
            result is a single plain-text message — never one carrying tool_calls
            — so pairing is preserved. Its output is capped, so the summary can
            never itself overflow the budget.

    Returns:
        A compacted history. If `history` already fits, the same list is returned
        unchanged (safe no-op).
    """
    if estimate_messages_tokens(history) <= budget_tokens:
        return history

    # ── Preamble: leading system messages + the first user goal ──────────────
    preamble: list[dict[str, Any]] = []
    index = 0
    while index < len(history) and history[index].get("role") == "system":
        preamble.append(history[index])
        index += 1
    if index < len(history) and history[index].get("role") == "user":
        preamble.append(history[index])
        index += 1

    body = history[index:]
    turns = split_into_turns(body)

    # Need at least one turn to summarize and one to keep.
    if len(turns) <= 1:
        return history

    # Condense the digest once (the summarizer call is the expensive part). We
    # always summarize at least the oldest turn; the digest covers everything not
    # kept, recomputed cheaply (deterministic) as we shrink the kept tail.
    async def build_summary(old_turns: list[list[dict[str, Any]]]) -> dict[str, Any]:
        digest = _default_summary(old_turns)
        if summarizer is not None:
            try:
                digest = await summarizer(digest)
            except Exception:
                digest = _default_summary(old_turns)
        return _summary_message(digest)

    max_keep = min(keep_recent_turns, len(turns) - 1)
    for keep in range(max_keep, 0, -1):
        recent_turns = turns[-keep:]
        old_turns = turns[:-keep]
        summary_message = await build_summary(old_turns)
        recent_messages = [message for turn in recent_turns for message in turn]
        candidate = [*preamble, summary_message, *recent_messages]
        if estimate_messages_tokens(candidate) <= budget_tokens:
            return candidate

    # Even keeping a single turn doesn't fit (e.g. one oversized turn). Best
    # effort: preamble + summary + the most recent turn. Per-tool-result
    # truncation upstream keeps a single turn bounded in practice.
    recent_messages = [message for message in turns[-1]]
    summary_message = await build_summary(turns[:-1])
    return [*preamble, summary_message, *recent_messages]
