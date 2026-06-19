from __future__ import annotations
"""
Risk registry — admin-editable per-org overrides for the Risk Pricer's base
factors. Inference (core/risk.py defaults) handles the long tail; admins override
the exceptions here without touching the model-facing tool schemas.

Overrides are read on the hot path (every tool call), so they are cached in
process with a short TTL. Writes invalidate the cache for the org.
"""
import logging
import time

log = logging.getLogger(__name__)

_TTL_SECONDS = 30.0
# org_id -> (expires_at, {tool: {"blast_radius": float, "irreversibility": float}})
_CACHE: dict[str, tuple[float, dict[str, dict[str, float]]]] = {}


async def get_overrides(org_id: str) -> dict[str, dict[str, float]]:
    cached = _CACHE.get(org_id)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    overrides = await _load(org_id)
    _CACHE[org_id] = (now + _TTL_SECONDS, overrides)
    return overrides


def invalidate(org_id: str) -> None:
    _CACHE.pop(org_id, None)


async def _load(org_id: str) -> dict[str, dict[str, float]]:
    try:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("risk_overrides")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(table.c.tool, table.c.blast_radius, table.c.irreversibility).where(
                        table.c.organization_id == org_id,
                        table.c.enabled.is_(True),
                    )
                )
            ).mappings().all()
        return {
            r["tool"]: {
                "blast_radius": float(r["blast_radius"]),
                "irreversibility": float(r["irreversibility"]),
            }
            for r in rows
        }
    except Exception as exc:  # table missing / no DB -> inference-only
        log.debug("risk overrides degraded: %s", exc)
        return {}


async def upsert(org_id: str, region: str, tool: str, blast_radius: float, irreversibility: float) -> None:
    from sqlalchemy import insert, update

    from core.db import engine, reflect_table

    table = await reflect_table("risk_overrides")
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                table.select().where(
                    table.c.organization_id == org_id, table.c.tool == tool
                )
            )
        ).mappings().first()
        values = dict(
            blast_radius=max(0.0, min(1.0, blast_radius)),
            irreversibility=max(0.0, min(1.0, irreversibility)),
            enabled=True,
        )
        if existing:
            await conn.execute(update(table).where(table.c.id == existing["id"]).values(**values))
        else:
            await conn.execute(
                insert(table).values(
                    organization_id=org_id, region=region, tool=tool, **values
                )
            )
    invalidate(org_id)


async def list_overrides(org_id: str) -> list[dict]:
    try:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("risk_overrides")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(table).where(table.c.organization_id == org_id).order_by(table.c.tool)
                )
            ).mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("risk overrides list degraded: %s", exc)
        return []
