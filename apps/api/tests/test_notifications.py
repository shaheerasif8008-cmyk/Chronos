"""W5.2 — in-app notification feed.

Proves durable, tenant-scoped notification records; the read/dismiss lifecycle;
org-wide vs targeted visibility; settings-gated emission; and that creation is
audited.

Requires DATABASE_URL pointing at a migrated Chronos database (defaults to the
local docker Postgres on :55432).
"""
import uuid

import pytest
from sqlalchemy import delete, insert, select

from core import notifications
from core.db import engine, reflect_table
from core.models import Member
from routers import notifications as notifications_router


async def _insert_org(org_id: str) -> None:
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(insert(orgs).values(id=org_id, slug=f"slug-{org_id}", name=f"Org {org_id}"))


async def _member(org_id: str, role: str = "user") -> Member:
    mid = f"m-{uuid.uuid4().hex[:8]}"
    email = f"{mid}@example.com"
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(
            insert(members).values(
                id=mid,
                organization_id=org_id,
                email=email,
                role=role,
            )
        )
    return Member(id=mid, organization_id=org_id, email=email, role=role)


async def _set_notification_settings(org_id: str, values: dict) -> None:
    table = await reflect_table("settings_documents")
    async with engine.begin() as conn:
        await conn.execute(insert(table).values(
            organization_id=org_id, region="us", scope="org", scope_id=org_id,
            section="notifications", values=values,
        ))


async def _cleanup(org_ids: list[str]) -> None:
    receipts = await reflect_table("notification_receipts")
    notif = await reflect_table("notifications")
    sdoc = await reflect_table("settings_documents")
    members = await reflect_table("members")
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(delete(receipts).where(receipts.c.organization_id.in_(org_ids)))
        await conn.execute(delete(notif).where(notif.c.organization_id.in_(org_ids)))
        await conn.execute(delete(sdoc).where(sdoc.c.organization_id.in_(org_ids)))
        await conn.execute(delete(members).where(members.c.organization_id.in_(org_ids)))
        await conn.execute(delete(orgs).where(orgs.c.id.in_(org_ids)))


@pytest.mark.asyncio
async def test_emit_list_read_dismiss_lifecycle_and_audit():
    org = f"orgN-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        member = await _member(org)
        nid = await notifications.emit(
            organization_id=org, type="approval_request", title="Approval needed",
            body="run gmail.draft", severity="warning", resource_type="task", resource_id="t1",
        )
        assert nid

        # Visible in the feed; counts as unread.
        feed = await notifications_router.list_notifications(limit=50, offset=0, member=member)
        assert any(n["id"] == nid for n in feed)
        assert (await notifications_router.unread_count(member=member))["count"] == 1

        # Creation is audited.
        audit_log = await reflect_table("audit_log")
        async with engine.begin() as conn:
            arows = (await conn.execute(select(audit_log).where(
                audit_log.c.organization_id == org,
                audit_log.c.action == "notification_created",
            ))).mappings().all()
        assert arows and arows[0]["resource_id"] == nid

        # Mark read → unread count drops, still in feed.
        await notifications_router.mark_read(
            body=notifications_router.NotificationIds(ids=[nid]), member=member
        )
        assert (await notifications_router.unread_count(member=member))["count"] == 0

        # Dismiss → excluded from default feed.
        await notifications_router.dismiss(
            body=notifications_router.NotificationIds(ids=[nid]), member=member
        )
        feed_after = await notifications_router.list_notifications(limit=50, offset=0, member=member)
        assert all(n["id"] != nid for n in feed_after)
        feed_incl = await notifications_router.list_notifications(include_dismissed=True, limit=50, offset=0, member=member)
        assert any(n["id"] == nid for n in feed_incl)
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_org_wide_visible_to_all_targeted_only_to_recipient():
    org = f"orgV-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        alice = await _member(org)
        bob = await _member(org)
        org_wide = await notifications.emit(organization_id=org, type="security", title="org-wide")
        targeted = await notifications.emit(
            organization_id=org, type="security", title="for-bob", member_id=str(bob.id)
        )

        alice_feed = {n["id"] for n in await notifications_router.list_notifications(limit=50, offset=0, member=alice)}
        bob_feed = {n["id"] for n in await notifications_router.list_notifications(limit=50, offset=0, member=bob)}

        assert org_wide in alice_feed and org_wide in bob_feed
        assert targeted in bob_feed and targeted not in alice_feed
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_org_wide_read_and_dismiss_state_is_per_member():
    org = f"orgR-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        alice = await _member(org)
        bob = await _member(org)
        nid = await notifications.emit(
            organization_id=org, type="security", title="shared alert"
        )
        assert nid

        # Alice reading a shared row cannot change Bob's unread state.
        assert await notifications.mark_read(org, str(alice.id), [nid]) == 1
        assert await notifications.unread_count(org, str(alice.id)) == 0
        assert await notifications.unread_count(org, str(bob.id)) == 1

        alice_feed = await notifications.list_for(org, str(alice.id))
        bob_feed = await notifications.list_for(org, str(bob.id))
        assert next(n for n in alice_feed if n["id"] == nid)["read_at"] is not None
        assert next(n for n in bob_feed if n["id"] == nid)["read_at"] is None

        # Alice dismissing it hides only her copy; Bob still sees the alert.
        assert await notifications.dismiss(org, str(alice.id), [nid]) == 1
        assert nid not in {n["id"] for n in await notifications.list_for(org, str(alice.id))}
        assert nid in {n["id"] for n in await notifications.list_for(org, str(bob.id))}

        receipts = await reflect_table("notification_receipts")
        notification_rows = await reflect_table("notifications")
        async with engine.begin() as conn:
            receipt_rows = (
                await conn.execute(
                    select(receipts).where(receipts.c.notification_id == nid)
                )
            ).mappings().all()
            base = (
                await conn.execute(
                    select(notification_rows).where(notification_rows.c.id == nid)
                )
            ).mappings().one()
        assert {r["member_id"] for r in receipt_rows} == {str(alice.id)}
        assert receipt_rows[0]["read_at"] is not None
        assert receipt_rows[0]["dismissed_at"] is not None
        # Deprecated shared columns are never mutated by member actions.
        assert base["read_at"] is None
        assert base["dismissed_at"] is None
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_receipt_mutations_reject_other_recipient_and_tenant_ids():
    org_a = f"orgRA-{uuid.uuid4().hex[:8]}"
    org_b = f"orgRB-{uuid.uuid4().hex[:8]}"
    await _insert_org(org_a)
    await _insert_org(org_b)
    try:
        alice = await _member(org_a)
        bob = await _member(org_a)
        mallory = await _member(org_b)
        for_bob = await notifications.emit(
            organization_id=org_a,
            type="security",
            title="bob only",
            member_id=str(bob.id),
        )
        in_other_tenant = await notifications.emit(
            organization_id=org_b,
            type="security",
            title="other tenant",
            member_id=str(mallory.id),
        )
        assert for_bob and in_other_tenant

        # IDs are untrusted input. Neither another recipient's targeted row nor
        # another tenant's row may produce a receipt for Alice.
        assert await notifications.mark_read(
            org_a, str(alice.id), [for_bob, in_other_tenant]
        ) == 0
        assert await notifications.dismiss(
            org_a, str(alice.id), [for_bob, in_other_tenant]
        ) == 0

        receipts = await reflect_table("notification_receipts")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(receipts).where(receipts.c.member_id == str(alice.id))
                )
            ).mappings().all()
        assert rows == []
        assert await notifications.unread_count(org_a, str(bob.id)) == 1
        assert await notifications.unread_count(org_b, str(mallory.id)) == 1
    finally:
        await _cleanup([org_a, org_b])


@pytest.mark.asyncio
async def test_emission_gated_by_org_settings():
    org = f"orgG-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        # Disable approval alerts for this org.
        await _set_notification_settings(org, {"approval_request_alerts": False})
        suppressed = await notifications.emit(
            organization_id=org, type="approval_request", title="should be suppressed"
        )
        assert suppressed is None
        # A different type is still allowed.
        allowed = await notifications.emit(organization_id=org, type="security", title="allowed")
        assert allowed is not None
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_master_in_app_switch_disables_all():
    org = f"orgM-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        await _set_notification_settings(org, {"in_app": False})
        assert await notifications.emit(organization_id=org, type="security", title="x") is None
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_feed_is_tenant_scoped():
    org_a = f"orgA-{uuid.uuid4().hex[:8]}"
    org_b = f"orgB-{uuid.uuid4().hex[:8]}"
    await _insert_org(org_a)
    await _insert_org(org_b)
    try:
        a_member = await _member(org_a)
        in_a = await notifications.emit(organization_id=org_a, type="security", title="A")
        await notifications.emit(organization_id=org_b, type="security", title="B")
        feed = await notifications_router.list_notifications(limit=50, offset=0, member=a_member)
        ids = {n["id"] for n in feed}
        assert in_a in ids
        assert {n["organization_id"] for n in feed} == {org_a}
    finally:
        await _cleanup([org_a, org_b])
