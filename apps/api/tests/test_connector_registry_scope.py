from __future__ import annotations

import pytest
from sqlalchemy import Column, MetaData, Table, Text
from sqlalchemy.ext.asyncio import create_async_engine

from core.models import AgentContext


def _connectors_table(*, explicit_member_scope: bool) -> Table:
    columns = [
        Column("id", Text, primary_key=True),
        Column("organization_id", Text, nullable=False),
        Column("provider", Text, nullable=False),
        Column("account_handle", Text),
        Column("vault_ref", Text, nullable=False),
        Column("status", Text, nullable=False),
        Column("persona_id", Text),
    ]
    if explicit_member_scope:
        columns.append(Column("member_id", Text))
    return Table("connectors", MetaData(), *columns)


@pytest.mark.asyncio
async def test_registry_prefers_owner_and_never_selects_peer_connector(monkeypatch):
    from connectors import registry

    table = _connectors_table(explicit_member_scope=True)
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with test_engine.begin() as conn:
        await conn.run_sync(table.metadata.create_all)
        await conn.execute(
            table.insert(),
            [
                {
                    "id": "gmail:acme:peer",
                    "organization_id": "acme",
                    "provider": "gmail",
                    "account_handle": "peer@example.com",
                    "vault_ref": "peer-secret",
                    "status": "active",
                    "persona_id": None,
                    "member_id": "peer",
                },
                {
                    "id": "gmail:acme:owner",
                    "organization_id": "acme",
                    "provider": "gmail",
                    "account_handle": "owner@example.com",
                    "vault_ref": "owner-secret",
                    "status": "active",
                    "persona_id": None,
                    "member_id": "owner",
                },
                {
                    "id": "gmail:acme:shared",
                    "organization_id": "acme",
                    "provider": "gmail",
                    "account_handle": "shared@example.com",
                    "vault_ref": "shared-secret",
                    "status": "active",
                    "persona_id": None,
                    "member_id": None,
                },
            ],
        )

    async def reflect_table(_name: str):
        return table

    monkeypatch.setattr(registry, "engine", test_engine)
    monkeypatch.setattr(registry, "reflect_table", reflect_table)
    try:
        record = await registry.get(
            AgentContext(id="agent", org_id="acme", member_id="owner"),
            "gmail.search",
        )
        assert record.vault_ref == "owner-secret"

        shared = await registry.get(
            AgentContext(id="agent", org_id="acme", member_id="new-member"),
            "gmail.search",
        )
        assert shared.vault_ref == "shared-secret"
    finally:
        await test_engine.dispose()


@pytest.mark.asyncio
async def test_registry_legacy_scope_handles_persona_without_local_or_shadow(monkeypatch):
    from connectors import registry

    table = _connectors_table(explicit_member_scope=False)
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with test_engine.begin() as conn:
        await conn.run_sync(table.metadata.create_all)
        await conn.execute(
            table.insert(),
            [
                {
                    "id": "slack:acme:owner",
                    "organization_id": "acme",
                    "provider": "slack",
                    "account_handle": "owner",
                    "vault_ref": "owner-secret",
                    "status": "active",
                    "persona_id": "researcher",
                },
                {
                    "id": "slack:acme:peer",
                    "organization_id": "acme",
                    "provider": "slack",
                    "account_handle": "peer",
                    "vault_ref": "peer-secret",
                    "status": "active",
                    "persona_id": "researcher",
                },
            ],
        )

    async def reflect_table(_name: str):
        return table

    monkeypatch.setattr(registry, "engine", test_engine)
    monkeypatch.setattr(registry, "reflect_table", reflect_table)
    try:
        record = await registry.get(
            AgentContext(
                id="agent",
                org_id="acme",
                member_id="owner",
                persona_id="researcher",
            ),
            "slack.search",
        )
        assert record.vault_ref == "owner-secret"
    finally:
        await test_engine.dispose()
