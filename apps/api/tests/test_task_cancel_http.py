"""HTTP cancel endpoint: a queued task can be cancelled (and executes no further
steps), the cancel is tenant-scoped, and a terminal task is not re-cancelled.

Deterministic: the task_runner worker is not started under ASGITransport, so a
created task stays queued and cannot execute on its own — letting us assert the
endpoint's status transition without racing a live runner.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

import main
from core.auth import create_access_token
from core.config import settings
from core.db import engine, reflect_table


async def _member_token(org_id: str) -> str:
    member_id = str(uuid.uuid4())
    members = await reflect_table("members")
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        # org_id may already exist (e.g. "default"); only insert when missing.
        existing = (
            await conn.execute(orgs.select().where(orgs.c.id == org_id))
        ).first()
        if not existing:
            await conn.execute(orgs.insert().values(id=org_id, slug=f"o-{org_id[:8]}", name="O"))
        await conn.execute(
            members.insert().values(
                id=member_id, organization_id=org_id, email=f"{member_id[:8]}@t.io", role="user"
            )
        )
    return create_access_token(member_id)


@pytest.mark.asyncio
async def test_http_cancel_is_tenant_scoped_and_stops_the_task():
    # Tasks are created under settings.org_id, so the creator/canceller live there.
    owner = await _member_token(settings.org_id)
    outsider = await _member_token(str(uuid.uuid4()))

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/tasks/",
            json={"goal": "cancel me"},
            headers={"Authorization": f"Bearer {owner}"},
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["task_id"]

        # Cross-tenant cancel is rejected (task not visible to another org).
        cross = await client.post(
            f"/tasks/{task_id}/cancel", headers={"Authorization": f"Bearer {outsider}"}
        )
        assert cross.status_code == 404, cross.text

        # Still cancellable by the owner; status flips to cancelled.
        ok = await client.post(
            f"/tasks/{task_id}/cancel", headers={"Authorization": f"Bearer {owner}"}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["cancelled"] is True
        assert ok.json()["status"] == "cancelled"

        detail = await client.get(
            f"/tasks/{task_id}", headers={"Authorization": f"Bearer {owner}"}
        )
        assert detail.json()["status"] == "cancelled"

        # Cancelling an already-terminal task is a no-op (not re-cancelled).
        again = await client.post(
            f"/tasks/{task_id}/cancel", headers={"Authorization": f"Bearer {owner}"}
        )
        assert again.json()["cancelled"] is False
        assert again.json()["status"] == "cancelled"
