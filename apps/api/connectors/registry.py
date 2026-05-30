from __future__ import annotations

"""ConnectorRegistry — looks up which connector record to use for a given tool call."""
from pydantic import BaseModel
from sqlalchemy import select

from core.db import engine, reflect_table
from core.exceptions import ConnectorNotFound
from core.models import AgentContext


class ConnectorRecord(BaseModel):
    id: str
    organization_id: str
    provider: str
    account_handle: str | None
    vault_ref: str
    status: str
    persona_id: str | None


def _provider_from_tool(tool_name: str) -> str:
    """Derive provider from tool name prefix: 'gmail.send' → 'gmail'."""
    return tool_name.split(".")[0]


async def get(agent: AgentContext, tool_name: str) -> ConnectorRecord:
    """Return the active connector for the org/provider implied by tool_name."""
    provider = _provider_from_tool(tool_name)
    connectors = await reflect_table("connectors")

    filters = [
        connectors.c.organization_id == agent.org_id,
        connectors.c.provider == provider,
        connectors.c.status == "active",
    ]
    if agent.persona_id:
        # Prefer persona-scoped connector; fall back to org-level below
        from sqlalchemy import or_
        filters.append(
            or_(connectors.c.persona_id == agent.persona_id, connectors.c.persona_id.is_(None))
        )

    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    connectors.c.id,
                    connectors.c.organization_id,
                    connectors.c.provider,
                    connectors.c.account_handle,
                    connectors.c.vault_ref,
                    connectors.c.status,
                    connectors.c.persona_id,
                )
                .where(*filters)
                # Persona-scoped connectors rank above org-level (NULL persona_id last)
                .order_by(connectors.c.persona_id.desc().nullslast())
                .limit(1)
            )
        ).mappings().all()

    if not rows:
        raise ConnectorNotFound(agent.org_id, provider)

    row = dict(rows[0])
    return ConnectorRecord(**row)


connector_registry = type("_Registry", (), {"get": staticmethod(get)})()
