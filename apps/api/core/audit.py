from __future__ import annotations
from typing import Any

from sqlalchemy import insert

from core.config import settings
from core.db import engine, reflect_table


async def log(
    event_type: str,
    actor_id: str | None,
    action: str,
    *,
    resource_type: str | None = None,
    resource_id: str | None = None,
    payload: dict[str, Any] | None = None,
    decision: str | None = None,
    organization_id: str | None = None,
    region: str | None = None,
) -> str:
    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(audit_log)
            .values(
                organization_id=organization_id or settings.org_id,
                region=region or settings.region,
                event_type=event_type,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                payload=payload,
                decision=decision,
            )
            .returning(audit_log.c.id)
        )
        return str(result.scalar_one())
