from __future__ import annotations

from datetime import date
from typing import Any

from core.config import settings
from core.redis import redis_client

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return max(1, len(str(text)) // CHARS_PER_TOKEN)


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        total += estimate_tokens(str(message.get("role") or ""))
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif content is not None:
            total += estimate_tokens(str(content))
        if message.get("tool_calls"):
            total += estimate_tokens(str(message["tool_calls"]))
        if message.get("name"):
            total += estimate_tokens(str(message["name"]))
    return total


def trim_to_token_budget(text: str | None, max_tokens: int, *, suffix: str = "...") -> str:
    value = str(text or "")
    if max_tokens <= 0:
        return ""
    if estimate_tokens(value) <= max_tokens:
        return value
    max_chars = max(0, (max_tokens * CHARS_PER_TOKEN) - len(suffix))
    return value[:max_chars].rstrip() + suffix


async def record_tokens_used(org_id: str, tokens: int) -> int:
    """Record observed model usage and return today's total for the org.

    This is intentionally independent of enforcement: usage is useful even when
    the daily limit is disabled.
    """
    if tokens <= 0:
        return await tokens_used_today(org_id)
    key = _token_key(org_id)
    count = await redis_client.incrby(key, int(tokens))
    if count == tokens:
        await redis_client.expire(key, 90_000)
    return int(count)


async def tokens_used_today(org_id: str) -> int:
    raw = await redis_client.get(_token_key(org_id))
    return int(raw) if raw else 0


async def token_usage_summary(org_id: str) -> dict[str, Any]:
    used = await tokens_used_today(org_id)
    return {
        "metered": used > 0,
        "tokens_today": used,
        "daily_limit": settings.per_org_daily_token_limit,
        "enforced": settings.per_org_daily_token_limit > 0,
    }


def _token_key(org_id: str) -> str:
    return f"tokens:{org_id}:{date.today().isoformat()}"
