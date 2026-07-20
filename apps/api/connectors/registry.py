from __future__ import annotations
"""ConnectorRegistry — looks up which connector record to use for a given tool call."""
from pydantic import BaseModel
from sqlalchemy import case, or_, select

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
    member_id = str(agent.member_id)

    filters = [
        connectors.c.organization_id == agent.org_id,
        connectors.c.provider == provider,
        connectors.c.status == "active",
    ]
    member_connector_id = f"{provider}:{agent.org_id}:{member_id}"
    if "member_id" in connectors.c:
        # Credentials are private to their owning member unless the connector
        # is explicitly org-shared (NULL owner). Never select another member's
        # row even when its legacy ID does not follow the expected convention.
        filters.append(
            or_(
                connectors.c.member_id == member_id,
                connectors.c.member_id.is_(None),
            )
        )
        member_rank = case((connectors.c.member_id == member_id, 0), else_=1)
    else:
        # Rolling-deploy compatibility while migration 0047 is not yet visible
        # to this process. Historical private rows use provider:org:member IDs.
        scoped_prefix = f"{provider}:{agent.org_id}:%"
        filters.append(
            or_(
                connectors.c.id == member_connector_id,
                ~connectors.c.id.like(scoped_prefix),
            )
        )
        member_rank = case((connectors.c.id == member_connector_id, 0), else_=1)
    if agent.persona_id:
        # Prefer persona-scoped connector; fall back to org-level below
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
                .order_by(
                    member_rank,
                    # Persona-scoped connectors rank above org-level (NULL persona_id last)
                    connectors.c.persona_id.desc().nullslast(),
                )
                .limit(1)
            )
        ).mappings().all()

    if not rows:
        raise ConnectorNotFound(agent.org_id, provider)

    row = dict(rows[0])
    return ConnectorRecord(**row)


connector_registry = type("_Registry", (), {"get": staticmethod(get)})()
