from __future__ import annotations

from connectors.framework.adapters import adapter_registry
from connectors.framework.repository import ConnectorRepository


async def seed_builtin_connectors(repo: ConnectorRepository, tenant_id: str = "default") -> None:
    for adapter in adapter_registry().values():
        await repo.upsert_connector_definition(adapter.connector, tenant_id=tenant_id)
