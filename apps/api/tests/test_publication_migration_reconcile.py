"""Regression proof for migration 0068's already-stamped database repair."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from core.db import engine


MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "0068_publication_constraint_reconcile.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0068", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _constraint_definitions(connection) -> dict[str, str]:
    rows = (
        await connection.execute(
            text(
                """
                SELECT conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'notification_delivery_receipts'::regclass
                  AND conname IN (
                    'ck_notification_delivery_channel',
                    'ck_notification_delivery_status'
                  )
                ORDER BY conname
                """
            )
        )
    ).all()
    return {str(name): str(definition) for name, definition in rows}


async def _approval_contract(connection) -> dict[str, bool]:
    row = (
        await connection.execute(
            text(
                """
                SELECT
                  EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'notification_delivery_receipts'
                      AND column_name = 'approval_id'
                  ),
                  EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'notification_delivery_receipts'::regclass
                      AND conname = 'fk_notification_delivery_approval'
                  ),
                  EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE tablename = 'notification_delivery_receipts'
                      AND indexname = 'ix_notification_delivery_approval'
                  )
                """
            )
        )
    ).one()
    return {"column": bool(row[0]), "foreign_key": bool(row[1]), "index": bool(row[2])}


@pytest.mark.asyncio
async def test_fresh_head_has_current_publication_delivery_contract() -> None:
    """CI's fresh Alembic chain must finish with the expanded checks."""

    async with engine.connect() as connection:
        definitions = await _constraint_definitions(connection)
        approval = await _approval_contract(connection)

    channel = definitions["ck_notification_delivery_channel"]
    status = definitions["ck_notification_delivery_status"]
    for value in ("email", "slack", "teams", "web", "api"):
        assert value in channel
    for value in (
        "approval_pending",
        "pending",
        "processing",
        "retry",
        "delivered",
        "dead_letter",
    ):
        assert value in status
    assert all(approval.values())


@pytest.mark.asyncio
async def test_upgrade_repairs_historical_checks_and_is_idempotent() -> None:
    """Simulate a DB stamped at 0067 with the pre-publication checks.

    PostgreSQL DDL is transactional, so the legacy constraints and both upgrade
    calls are rolled back after the proof and cannot leak into other tests.
    """

    migration = _load_migration()
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:

            def install_legacy_checks(sync_connection) -> None:
                operations = Operations(MigrationContext.configure(sync_connection))
                # Other publication tests intentionally leave durable receipts.
                # Remove rows the historical contract could never have stored;
                # the surrounding transaction restores them after this proof.
                operations.execute(
                    "DELETE FROM notification_delivery_receipts "
                    "WHERE channel IN ('web','api') OR status = 'approval_pending'"
                )
                operations.drop_index(
                    "ix_notification_delivery_approval",
                    table_name="notification_delivery_receipts",
                )
                operations.drop_constraint(
                    "fk_notification_delivery_approval",
                    "notification_delivery_receipts",
                    type_="foreignkey",
                )
                operations.drop_column(
                    "notification_delivery_receipts",
                    "approval_id",
                )
                operations.drop_constraint(
                    "ck_notification_delivery_channel",
                    "notification_delivery_receipts",
                    type_="check",
                )
                operations.create_check_constraint(
                    "ck_notification_delivery_channel",
                    "notification_delivery_receipts",
                    "channel IN ('email','slack','teams')",
                )
                operations.drop_constraint(
                    "ck_notification_delivery_status",
                    "notification_delivery_receipts",
                    type_="check",
                )
                operations.create_check_constraint(
                    "ck_notification_delivery_status",
                    "notification_delivery_receipts",
                    "status IN ('pending','processing','retry','delivered','dead_letter')",
                )
                migration.op = operations

            await connection.run_sync(install_legacy_checks)
            legacy = await _constraint_definitions(connection)
            assert "web" not in legacy["ck_notification_delivery_channel"]
            assert "approval_pending" not in legacy["ck_notification_delivery_status"]
            assert not any((await _approval_contract(connection)).values())

            await connection.run_sync(lambda _: migration.upgrade())
            await connection.run_sync(lambda _: migration.upgrade())

            repaired = await _constraint_definitions(connection)
            assert "web" in repaired["ck_notification_delivery_channel"]
            assert "api" in repaired["ck_notification_delivery_channel"]
            assert "approval_pending" in repaired["ck_notification_delivery_status"]
            assert all((await _approval_contract(connection)).values())
        finally:
            await transaction.rollback()


def test_migration_is_linear_and_forward_only() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision = "0067_file_quarantine_review"' in source
    assert "forward-compatible no-op" in source
