from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from core.redis import redis_client
from core.token_budget import record_tokens_used, tokens_used_today


class GovernanceLimitExceeded(Exception):
    """Raised when org-level safety/cost governance blocks work."""


@dataclass(frozen=True)
class GovernanceConfig:
    daily_token_limit: int = 0
    daily_cost_limit_usd: float = 0.0
    request_rate_per_minute: int = 60
    connector_rate_per_minute: int = 60
    max_task_queue_size: int = 100
    max_concurrent_runtimes: int = 3


_MODEL_COST_PER_1K_TOKENS_USD: dict[str, float] = {
    "openrouter/openai/gpt-5.4-mini": 0.0025,
    "openrouter/openai/gpt-5.4-nano": 0.0005,
    "openrouter/deepseek/deepseek-v4-pro": 0.0015,
    "openrouter/deepseek/deepseek-v4-flash": 0.0002,
}
_DEFAULT_COST_PER_1K_TOKENS_USD = 0.002
_SUSPENSION_TTL_SECONDS = 90_000


def _today() -> str:
    return date.today().isoformat()


def _cost_key(org_id: str) -> str:
    return f"cost_usd_micros:{org_id}:{_today()}"


def _suspended_key(org_id: str) -> str:
    return f"governance:suspended:{org_id}"


def _rate_key(org_id: str, scope: str, window: int) -> str:
    return f"governance:rate:{scope}:{org_id}:{window}"


def _positive_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _positive_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, parsed)


async def governance_config(org_id: str) -> GovernanceConfig:
    try:
        from core.settings_store import get_settings_doc

        member = Member(id="system", organization_id=org_id, region=settings.region, email="system@local", role="admin")
        runtime = await get_settings_doc(member, "runtime", scope="org", scope_id=org_id)
        ai_employee = await get_settings_doc(member, "ai_employee", scope="org", scope_id=org_id)
    except Exception:
        runtime = {}
        ai_employee = {}
    token_limit = _positive_int(runtime.get("token_budget_daily"), settings.per_org_daily_token_limit)
    if settings.per_org_daily_token_limit > 0:
        token_limit = settings.per_org_daily_token_limit
    return GovernanceConfig(
        daily_token_limit=token_limit,
        daily_cost_limit_usd=_positive_float(runtime.get("cost_budget_daily_usd"), 0.0),
        request_rate_per_minute=_positive_int(runtime.get("request_rate_per_minute"), 60),
        connector_rate_per_minute=_positive_int(runtime.get("connector_rate_per_minute"), 60),
        max_task_queue_size=_positive_int(runtime.get("max_task_queue_size"), 100),
        max_concurrent_runtimes=_positive_int(ai_employee.get("max_concurrent_runtimes"), 3),
    )


def estimate_model_cost_usd(model: str | None, total_tokens: int) -> float:
    if total_tokens <= 0:
        return 0.0
    normalized = (model or "").split(":", 1)[0]
    rate = _MODEL_COST_PER_1K_TOKENS_USD.get(normalized, _DEFAULT_COST_PER_1K_TOKENS_USD)
    return (int(total_tokens) / 1000.0) * rate


async def cost_used_today_usd(org_id: str) -> float:
    try:
        raw = await redis_client.get(_cost_key(org_id))
    except Exception:
        return 0.0
    micros = int(raw) if raw else 0
    return micros / 1_000_000.0


async def is_org_suspended(org_id: str) -> bool:
    try:
        return bool(await redis_client.get(_suspended_key(org_id)))
    except Exception:
        return False


async def suspend_org(org_id: str, reason: str, *, actor_id: str = "system") -> None:
    try:
        await redis_client.set(_suspended_key(org_id), reason, ex=_SUSPENSION_TTL_SECONDS)
    except Exception:
        return
    try:
        await audit.log(
            "abuse_circuit_breaker",
            actor_id,
            "governance.suspend_org",
            organization_id=org_id,
            resource_type="organization",
            resource_id=org_id,
            payload={"reason": reason},
            decision="suspended",
        )
    except Exception:
        pass


async def usage_summary(org_id: str) -> dict[str, Any]:
    cfg = await governance_config(org_id)
    try:
        tokens = await tokens_used_today(org_id)
    except Exception:
        tokens = 0
    cost = await cost_used_today_usd(org_id)
    cost_warn = bool(cfg.daily_cost_limit_usd and cost >= cfg.daily_cost_limit_usd * 0.8)
    token_warn = bool(cfg.daily_token_limit and tokens >= cfg.daily_token_limit * 0.8)
    suspended = await is_org_suspended(org_id)
    return {
        "tokens": {
            "metered": tokens > 0,
            "tokens_today": tokens,
            "daily_limit": cfg.daily_token_limit,
            "enforced": cfg.daily_token_limit > 0,
            "warning": token_warn,
        },
        "cost": {
            "metered": cost > 0,
            "cost_today_usd": round(cost, 6),
            "daily_limit_usd": cfg.daily_cost_limit_usd,
            "enforced": cfg.daily_cost_limit_usd > 0,
            "warning": cost_warn,
            "budget_hard_stop": bool(cfg.daily_cost_limit_usd and cost >= cfg.daily_cost_limit_usd),
        },
        "suspended": suspended,
    }


async def record_model_usage(
    org_id: str,
    *,
    model: str | None,
    total_tokens: int,
    prompt_tokens: int = 0,
) -> dict[str, Any]:
    try:
        tokens_today = await record_tokens_used(org_id, int(total_tokens))
    except Exception:
        tokens_today = 0
    cost_micros = int(round(estimate_model_cost_usd(model, int(total_tokens)) * 1_000_000))
    if cost_micros > 0:
        try:
            count = await redis_client.incrby(_cost_key(org_id), cost_micros)
            if count == cost_micros:
                await redis_client.expire(_cost_key(org_id), _SUSPENSION_TTL_SECONDS)
        except Exception:
            pass
    cfg = await governance_config(org_id)
    cost_today = await cost_used_today_usd(org_id)
    hard_stop = bool(cfg.daily_cost_limit_usd and cost_today >= cfg.daily_cost_limit_usd)
    if hard_stop:
        await suspend_org(org_id, "daily cost budget exhausted", actor_id="model_meter")
    return {
        "tokens_today": tokens_today,
        "total_tokens": int(total_tokens),
        "prompt_tokens": int(prompt_tokens),
        "cost_today_usd": round(cost_today, 6),
        "cost_delta_usd": round(cost_micros / 1_000_000.0, 6),
        "daily_cost_limit_usd": cfg.daily_cost_limit_usd,
        "budget_hard_stop": hard_stop,
        "suspended": await is_org_suspended(org_id),
    }


async def enforce_model_budget(org_id: str, *, model: str | None, estimated_tokens: int = 0) -> None:
    if await is_org_suspended(org_id):
        raise GovernanceLimitExceeded("organization suspended: cost budget or abuse circuit breaker is active")
    cfg = await governance_config(org_id)
    try:
        tokens = await tokens_used_today(org_id)
    except Exception:
        tokens = 0
    if cfg.daily_token_limit and tokens + max(0, int(estimated_tokens)) >= cfg.daily_token_limit:
        raise GovernanceLimitExceeded(
            f"daily token budget exhausted for org {org_id}: {tokens}/{cfg.daily_token_limit}"
        )
    if cfg.daily_cost_limit_usd:
        projected = await cost_used_today_usd(org_id) + estimate_model_cost_usd(model, max(0, int(estimated_tokens)))
        if projected >= cfg.daily_cost_limit_usd:
            raise GovernanceLimitExceeded(
                f"daily cost budget exhausted for org {org_id}: ${projected:.6f}/${cfg.daily_cost_limit_usd:.6f}"
            )


async def enforce_request_rate(org_id: str, *, scope: str = "request") -> None:
    if await is_org_suspended(org_id):
        raise GovernanceLimitExceeded("organization suspended: abuse circuit breaker is active")
    cfg = await governance_config(org_id)
    limit = cfg.connector_rate_per_minute if scope == "connector" else cfg.request_rate_per_minute
    if limit <= 0:
        return
    import time

    window = int(time.time() / 60)
    key = _rate_key(org_id, scope, window)
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, 120)
    except Exception:
        return
    if count > limit:
        if count >= limit * 3:
            await suspend_org(org_id, f"{scope} rate limit runaway: {count}/{limit}", actor_id="rate_limiter")
        raise GovernanceLimitExceeded(f"{scope} rate limit exceeded for org {org_id}: {count}/{limit}/min")


async def _task_counts(org_id: str) -> dict[str, int]:
    try:
        tasks = await reflect_table("tasks")
        async with engine.begin() as conn:
            queued = (
                await conn.execute(
                    select(func.count()).select_from(tasks).where(
                        tasks.c.organization_id == org_id,
                        tasks.c.status == "queued",
                        tasks.c.parent_task_id.is_(None),
                    )
                )
            ).scalar_one()
            active = (
                await conn.execute(
                    select(func.count()).select_from(tasks).where(
                        tasks.c.organization_id == org_id,
                        tasks.c.status.in_(["planning", "running"]),
                    )
                )
            ).scalar_one()
    except Exception:
        return {"queued": 0, "active": 0}
    return {"queued": int(queued), "active": int(active)}


async def enforce_task_admission(org_id: str) -> None:
    if await is_org_suspended(org_id):
        raise GovernanceLimitExceeded("organization suspended: task creation is blocked")
    cfg = await governance_config(org_id)
    counts = await _task_counts(org_id)
    if cfg.max_task_queue_size and counts["queued"] >= cfg.max_task_queue_size:
        raise GovernanceLimitExceeded(
            f"task queue limit exceeded for org {org_id}: {counts['queued']}/{cfg.max_task_queue_size}"
        )


async def enforce_task_start(org_id: str, task_id: str | None = None) -> None:
    if await is_org_suspended(org_id):
        raise GovernanceLimitExceeded("organization suspended: task execution is blocked")
    cfg = await governance_config(org_id)
    if cfg.max_concurrent_runtimes <= 0:
        return
    counts = await _task_counts(org_id)
    if counts["active"] > cfg.max_concurrent_runtimes:
        await suspend_org(org_id, "concurrent runtime cap exceeded", actor_id="task_runner")
        raise GovernanceLimitExceeded(
            f"concurrent runtime limit exceeded for org {org_id}: {counts['active']}/{cfg.max_concurrent_runtimes}"
        )
