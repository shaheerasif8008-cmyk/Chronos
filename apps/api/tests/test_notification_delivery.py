"""W5.3 — notification delivery channels.

Proves truthful-degraded email delivery (no provider → degraded, never marks
emailed), real delivery when a provider is monkeypatched, the per-org email
toggle gate, and the unread digest rollup.

Requires DATABASE_URL pointing at a migrated Chronos database (defaults to the
local docker Postgres on :55432).
"""
import uuid

import pytest
from sqlalchemy import delete, insert, select

from core import notification_delivery, notifications
from core.db import engine, reflect_table
from core.models import Member


async def _insert_org(org_id: str) -> None:
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(insert(orgs).values(id=org_id, slug=f"slug-{org_id}", name=f"Org {org_id}"))


async def _insert_member(org_id: str, role: str) -> str:
    mid = f"m-{uuid.uuid4().hex[:8]}"
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(insert(members).values(
            id=mid, organization_id=org_id, email=f"{mid}@example.com", role=role,
        ))
    return mid


async def _set_notification_settings(org_id: str, values: dict) -> None:
    table = await reflect_table("settings_documents")
    async with engine.begin() as conn:
        await conn.execute(insert(table).values(
            organization_id=org_id, region="us", scope="org", scope_id=org_id,
            section="notifications", values=values,
        ))


async def _cleanup(org_ids: list[str]) -> None:
    notif = await reflect_table("notifications")
    sdoc = await reflect_table("settings_documents")
    members = await reflect_table("members")
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(delete(notif).where(notif.c.organization_id.in_(org_ids)))
        await conn.execute(delete(sdoc).where(sdoc.c.organization_id.in_(org_ids)))
        await conn.execute(delete(members).where(members.c.organization_id.in_(org_ids)))
        await conn.execute(delete(orgs).where(orgs.c.id.in_(org_ids)))


@pytest.mark.asyncio
async def test_delivery_skipped_when_email_disabled():
    org = f"orgD1-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        # Default settings: email is False.
        await notifications.emit(organization_id=org, type="security", title="x")
        result = await notification_delivery.deliver_pending(org)
        assert result["status"] == "skipped"
        assert result["reason"] == "email_disabled_for_org"
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_delivery_degraded_when_no_provider(monkeypatch):
    org = f"orgD2-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        await _set_notification_settings(org, {"email": True})
        monkeypatch.setattr(notification_delivery, "email_is_configured", lambda: False)
        await notifications.emit(organization_id=org, type="security", title="x")
        result = await notification_delivery.deliver_pending(org)
        assert result["status"] == "degraded"
        assert result["pending"] == 1
        # Truthful-degraded: emailed_at must NOT be set.
        table = await reflect_table("notifications")
        async with engine.begin() as conn:
            rows = (await conn.execute(select(table).where(table.c.organization_id == org))).mappings().all()
        assert all(r["emailed_at"] is None for r in rows)
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_delivery_sends_and_marks_emailed_when_configured(monkeypatch):
    org = f"orgD3-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        await _insert_member(org, "owner")
        await _set_notification_settings(org, {"email": True})
        monkeypatch.setattr(notification_delivery, "email_is_configured", lambda: True)
        sent: list[dict] = []
        monkeypatch.setattr(
            notification_delivery, "_provider_send_email",
            lambda **kw: sent.append(kw),
        )
        await notifications.emit(organization_id=org, type="security", title="org-wide alert")
        result = await notification_delivery.deliver_pending(org)
        assert result["status"] == "ok"
        assert result["delivered"] == 1
        assert sent and sent[0]["subject"] == "org-wide alert"
        # emailed_at is now set, so a second run delivers nothing.
        again = await notification_delivery.deliver_pending(org)
        assert again["delivered"] == 0
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_digest_groups_unread_by_type():
    org = f"orgD4-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        member_id = await _insert_member(org, "user")
        await notifications.emit(organization_id=org, type="security", title="a")
        await notifications.emit(organization_id=org, type="security", title="b")
        await notifications.emit(organization_id=org, type="task_failure", title="c")
        digest = await notification_delivery.build_digest(org, member_id)
        assert digest["unread_total"] == 3
        assert digest["by_type"] == {"security": 2, "task_failure": 1}
    finally:
        await _cleanup([org])
