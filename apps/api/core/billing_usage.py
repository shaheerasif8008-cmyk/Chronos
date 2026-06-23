"""Persistent per-org monthly usage ledger for billing (W4.3).

Survives Redis counter expiry so billing periods can be computed accurately.
Each row is one (organization_id, period) pair; tokens and cost are accumulated
via an upsert on the unique index.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.config import settings
from core.db import engine, reflect_table
from core.plans import get_entitlements


def current_period() -> str:
    """Return the current billing period as 'YYYY-MM' (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def record(org_id: str, *, tokens: int, cost_usd: float, period: str | None = None) -> None:
    """Upsert this period's (org) usage, adding tokens + cost.

    Args:
        org_id: The organization UUID.
        tokens: Per-call token count to add (NOT a running total).
        cost_usd: Per-call cost in USD to add (NOT a running total).
        period: Billing period string ('YYYY-MM'). Defaults to current UTC month.
    """
    period = period or current_period()
    table = await reflect_table("usage_records")
    stmt = pg_insert(table).values(
        organization_id=org_id,
        region=settings.region,
        period=period,
        tokens=tokens,
        cost_usd=cost_usd,
    ).on_conflict_do_update(
        index_elements=["organization_id", "period"],
        set_={
            "tokens": table.c.tokens + tokens,
            "cost_usd": table.c.cost_usd + cost_usd,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)


async def monthly_usage(org_id: str, period: str | None = None) -> dict:
    """Return the accumulated usage for an org's billing period.

    Returns a dict with keys: period, tokens, cost_usd.
    If no row exists for this period, returns zeros.

    Args:
        org_id: The organization UUID.
        period: Billing period string ('YYYY-MM'). Defaults to current UTC month.
    """
    period = period or current_period()
    table = await reflect_table("usage_records")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table).where(
                    table.c.organization_id == org_id,
                    table.c.period == period,
                )
            )
        ).mappings().first()
    return {
        "period": period,
        "tokens": int(row["tokens"]) if row else 0,
        "cost_usd": float(row["cost_usd"]) if row else 0.0,
    }
