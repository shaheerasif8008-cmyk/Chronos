from __future__ import annotations
from typing import Any

from sqlalchemy import insert

from core.audit_redaction import redact
from core.config import settings
from core.db import engine, reflect_table


async def log(
    event_type: str,
    actor_id: str | None,
    action: str,
    *,
    organization_id: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    payload: dict[str, Any] | None = None,
    decision: str | None = None,
) -> str:
    """Append an audit entry.

    ``organization_id`` is the tenant the event belongs to (the actor's org) and
    is required — never default it to the process-wide ``settings.org_id``, or
    entries written for a non-default tenant become invisible to that tenant.
    """
    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(audit_log)
            .values(
                organization_id=organization_id,
                region=settings.region,
                event_type=event_type,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                payload=redact(payload) if payload is not None else None,
                decision=decision,
            )
            .returning(audit_log.c.id)
        )
        return str(result.scalar_one())
