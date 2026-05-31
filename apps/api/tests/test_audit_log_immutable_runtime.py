"""RULE 6 enforced at runtime: UPDATE and DELETE on audit_log are rejected by
the database trigger, not merely absent from migration grants.

The existing tests/test_governance_invariants.py check only that migrations add
no UPDATE/DELETE grants (static text). This proves the trigger actually fires.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, insert, update

from core.db import engine, reflect_table


async def _insert_audit_row() -> str:
    row_id = str(uuid.uuid4())
    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        await conn.execute(
            insert(audit_log).values(
                id=row_id,
                organization_id="default",
                event_type="test_event",
                action="test.action",
            )
        )
    return row_id


@pytest.mark.asyncio
async def test_audit_log_update_is_rejected_by_trigger():
    row_id = await _insert_audit_row()
    audit_log = await reflect_table("audit_log")
    with pytest.raises(Exception) as exc:
        async with engine.begin() as conn:
            await conn.execute(
                update(audit_log).where(audit_log.c.id == row_id).values(action="tampered")
            )
    assert "append-only" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_audit_log_delete_is_rejected_by_trigger():
    row_id = await _insert_audit_row()
    audit_log = await reflect_table("audit_log")
    with pytest.raises(Exception) as exc:
        async with engine.begin() as conn:
            await conn.execute(delete(audit_log).where(audit_log.c.id == row_id))
    assert "append-only" in str(exc.value).lower()
