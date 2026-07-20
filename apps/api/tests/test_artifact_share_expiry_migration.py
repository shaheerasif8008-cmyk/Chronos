"""Regression proof for forward-only public share expiry migration 0069."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from core.db import engine


MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "0069_artifact_share_expiry.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0069", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _expiry_contract(connection) -> tuple[bool, bool, bool, bool]:
    row = (
        await connection.execute(
            text(
                """
                SELECT
                  EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'artifact_shares' AND column_name = 'expires_at'
                  ),
                  EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE tablename = 'artifact_shares' AND indexname = 'ix_artifact_shares_expiry'
                  ),
                  EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'artifact_shares'
                      AND column_name = 'expires_at'
                      AND column_default IS NOT NULL
                  ),
                  EXISTS (
                    SELECT 1
                      FROM pg_constraint
                     WHERE conname = 'ck_artifact_shares_active_expires'
                  )
                """
            )
        )
    ).one()
    return bool(row[0]), bool(row[1]), bool(row[2]), bool(row[3])


@pytest.mark.asyncio
async def test_fresh_head_has_artifact_share_expiry_contract() -> None:
    async with engine.connect() as connection:
        assert await _expiry_contract(connection) == (True, True, True, True)


@pytest.mark.asyncio
async def test_upgrade_repairs_historical_share_table_and_is_idempotent() -> None:
    migration = _load_migration()
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            def install_historical_schema(sync_connection) -> None:
                operations = Operations(MigrationContext.configure(sync_connection))
                inspector = inspect(sync_connection)
                index_names = {
                    index["name"]
                    for index in inspector.get_indexes("artifact_shares")
                }
                constraint_names = {
                    constraint["name"]
                    for constraint in inspector.get_check_constraints("artifact_shares")
                }
                if "ix_artifact_shares_expiry" in index_names:
                    operations.drop_index(
                        "ix_artifact_shares_expiry",
                        table_name="artifact_shares",
                    )
                if "ck_artifact_shares_active_expires" in constraint_names:
                    operations.drop_constraint(
                        "ck_artifact_shares_active_expires",
                        "artifact_shares",
                        type_="check",
                    )
                operations.drop_column("artifact_shares", "expires_at")
                migration.op = operations

            await connection.run_sync(install_historical_schema)
            assert await _expiry_contract(connection) == (False, False, False, False)
            historical_token = f"historical-{uuid4().hex}"
            await connection.execute(
                text(
                    """
                    INSERT INTO artifact_shares
                        (organization_id, artifact_id, token, visibility, status)
                    VALUES
                        ('migration-test', :artifact_id, :token, 'public_link', 'active')
                    """
                ),
                {"artifact_id": uuid4(), "token": historical_token},
            )
            await connection.run_sync(lambda _: migration.upgrade())
            await connection.run_sync(lambda _: migration.upgrade())
            assert await _expiry_contract(connection) == (True, True, True, True)
            assert (
                await connection.scalar(
                    text(
                        "SELECT expires_at FROM artifact_shares WHERE token = :token"
                    ),
                    {"token": historical_token},
                )
            ) is not None

            rolling_token = f"rolling-{uuid4().hex}"
            await connection.execute(
                text(
                    """
                    INSERT INTO artifact_shares
                        (organization_id, artifact_id, token, visibility, status)
                    VALUES
                        ('migration-test', :artifact_id, :token, 'public_link', 'active')
                    """
                ),
                {"artifact_id": uuid4(), "token": rolling_token},
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT expires_at FROM artifact_shares WHERE token = :token"
                    ),
                    {"token": rolling_token},
                )
            ) is not None
        finally:
            await transaction.rollback()


def test_migration_is_linear_and_forward_only() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision = "0068_publication_reconcile"' in source
    assert "instead of widening public access" in source


@pytest.mark.asyncio
async def test_share_ttl_is_validated_before_database_access() -> None:
    from core.artifact_shares import create_share
    from core.config import settings

    with pytest.raises(ValueError, match="Public link duration"):
        await create_share("not-read", org_id="not-read", expires_in_hours=0)
    with pytest.raises(ValueError, match="Public link duration"):
        await create_share(
            "not-read",
            org_id="not-read",
            expires_in_hours=settings.artifact_share_ttl_hours + 1,
        )
