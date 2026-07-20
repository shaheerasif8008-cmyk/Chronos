"""W5.3 — notification delivery channels.

Proves truthful-degraded email delivery (no provider → degraded, never marks
emailed), real delivery when a provider is monkeypatched, the per-org email
toggle gate, and the unread digest rollup.

Requires DATABASE_URL pointing at a migrated Chronos database (defaults to the
local docker Postgres on :55432).
"""

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, insert, select, update

from core import notification_delivery, notifications
from core.db import engine, reflect_table


def test_sendgrid_provider_posts_real_mail_payload(monkeypatch):
    monkeypatch.setattr(notification_delivery.settings, "sendgrid_api_key", "sg-test")
    monkeypatch.setattr(
        notification_delivery.settings, "notification_from_email", "chronos@example.com"
    )
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return httpx.Response(202, request=httpx.Request("POST", url))

    monkeypatch.setattr(notification_delivery.httpx, "post", fake_post)
    notification_delivery._provider_send_email(
        to="person@example.com", subject="Welcome", body="Invitation link"
    )
    assert captured["url"] == "https://api.sendgrid.com/v3/mail/send"
    assert (
        captured["json"]["personalizations"][0]["to"][0]["email"]
        == "person@example.com"
    )
    assert captured["json"]["from"]["email"] == "chronos@example.com"


async def _insert_org(org_id: str) -> None:
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(
            insert(orgs).values(id=org_id, slug=f"slug-{org_id}", name=f"Org {org_id}")
        )


async def _insert_member(org_id: str, role: str) -> str:
    mid = f"m-{uuid.uuid4().hex[:8]}"
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(
            insert(members).values(
                id=mid,
                organization_id=org_id,
                email=f"{mid}@example.com",
                role=role,
            )
        )
    return mid


async def _set_notification_settings(org_id: str, values: dict) -> None:
    table = await reflect_table("settings_documents")
    async with engine.begin() as conn:
        await conn.execute(
            insert(table).values(
                organization_id=org_id,
                region="us",
                scope="org",
                scope_id=org_id,
                section="notifications",
                values=values,
            )
        )


async def _cleanup(org_ids: list[str]) -> None:
    delivery = await reflect_table("notification_delivery_receipts")
    receipts = await reflect_table("notification_receipts")
    notif = await reflect_table("notifications")
    sdoc = await reflect_table("settings_documents")
    members = await reflect_table("members")
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(
            delete(delivery).where(delivery.c.organization_id.in_(org_ids))
        )
        await conn.execute(
            delete(receipts).where(receipts.c.organization_id.in_(org_ids))
        )
        await conn.execute(delete(notif).where(notif.c.organization_id.in_(org_ids)))
        await conn.execute(delete(sdoc).where(sdoc.c.organization_id.in_(org_ids)))
        await conn.execute(
            delete(members).where(members.c.organization_id.in_(org_ids))
        )
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
        assert result["reason"] == "external_notifications_disabled_for_org"
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_delivery_degraded_when_no_provider(monkeypatch):
    org = f"orgD2-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        await _insert_member(org, "owner")
        await _set_notification_settings(org, {"email": True})
        monkeypatch.setattr(notification_delivery, "email_is_configured", lambda: False)
        await notifications.emit(organization_id=org, type="security", title="x")
        result = await notification_delivery.deliver_pending(org)
        assert result["status"] == "degraded"
        assert result["pending"] == 1
        # Truthful-degraded: emailed_at must NOT be set.
        table = await reflect_table("notifications")
        async with engine.begin() as conn:
            rows = (
                (
                    await conn.execute(
                        select(table).where(table.c.organization_id == org)
                    )
                )
                .mappings()
                .all()
            )
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
            notification_delivery,
            "_provider_send_email",
            lambda **kw: sent.append(kw),
        )
        await notifications.emit(
            organization_id=org, type="security", title="org-wide alert"
        )
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


@pytest.mark.asyncio
async def test_delivery_retry_is_durable_and_honors_backoff(monkeypatch):
    org = f"orgD5-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        await _insert_member(org, "owner")
        await _set_notification_settings(org, {"email": True})
        monkeypatch.setattr(notification_delivery, "email_is_configured", lambda: True)
        attempts = 0

        def fail_once(**_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise notification_delivery.EmailDeliveryError(
                    "email_provider_http_503"
                )
            return "provider-message-1"

        monkeypatch.setattr(notification_delivery, "_provider_send_email", fail_once)
        await notifications.emit(organization_id=org, type="security", title="retry me")
        started = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)

        first = await notification_delivery.deliver_pending(org, now=started)
        assert first["retried"] == 1
        assert first["delivered"] == 0

        too_early = await notification_delivery.deliver_pending(
            org, now=started + timedelta(seconds=30)
        )
        assert too_early["delivered"] == 0
        assert attempts == 1

        recovered = await notification_delivery.deliver_pending(
            org, now=started + timedelta(seconds=61)
        )
        assert recovered["delivered"] == 1
        assert attempts == 2

        delivery = await reflect_table("notification_delivery_receipts")
        async with engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        select(delivery).where(delivery.c.organization_id == org)
                    )
                )
                .mappings()
                .one()
            )
        assert row["status"] == "delivered"
        assert row["attempts"] == 2
        assert row["provider_message_id"] == "provider-message-1"
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_delivery_dead_letters_after_bounded_attempts(monkeypatch):
    org = f"orgD6-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        await _insert_member(org, "owner")
        await _set_notification_settings(org, {"email": True})
        monkeypatch.setattr(notification_delivery, "email_is_configured", lambda: True)
        monkeypatch.setattr(
            notification_delivery,
            "_provider_send_email",
            lambda **_kwargs: (_ for _ in ()).throw(
                notification_delivery.EmailDeliveryError("email_provider_http_503")
            ),
        )
        await notifications.emit(
            organization_id=org, type="security", title="eventually dead"
        )
        started = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
        result = {}
        for attempt in range(notification_delivery.MAX_ATTEMPTS):
            result = await notification_delivery.deliver_pending(
                org, now=started + timedelta(days=attempt)
            )
        assert result["dead_letter"] == 1
        assert result["pending"] == 0
        assert result["dead_letter_total"] == 1

        delivery = await reflect_table("notification_delivery_receipts")
        notification_rows = await reflect_table("notifications")
        async with engine.begin() as conn:
            receipt = (
                (
                    await conn.execute(
                        select(delivery).where(delivery.c.organization_id == org)
                    )
                )
                .mappings()
                .one()
            )
            notification = (
                (
                    await conn.execute(
                        select(notification_rows).where(
                            notification_rows.c.organization_id == org
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert receipt["status"] == "dead_letter"
        assert receipt["attempts"] == notification_delivery.MAX_ATTEMPTS
        assert receipt["last_error_code"] == "email_provider_http_503"
        assert notification["emailed_at"] is None
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_concurrent_dispatchers_claim_each_recipient_once(monkeypatch):
    org = f"orgD7-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        await _insert_member(org, "owner")
        await _set_notification_settings(org, {"email": True})
        monkeypatch.setattr(notification_delivery, "email_is_configured", lambda: True)
        sent: list[str] = []

        def send(**kwargs):
            sent.append(kwargs["delivery_key"])
            time.sleep(0.05)

        monkeypatch.setattr(notification_delivery, "_provider_send_email", send)
        await notifications.emit(
            organization_id=org, type="security", title="only once"
        )
        results = await asyncio.gather(
            notification_delivery.deliver_pending(org),
            notification_delivery.deliver_pending(org),
        )
        assert sum(result["delivered"] for result in results) == 1
        assert len(sent) == 1
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_automatic_delivery_cycle_invokes_durable_dispatch(monkeypatch):
    org = f"orgD9-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        await _insert_member(org, "owner")
        await _set_notification_settings(org, {"email": True})
        monkeypatch.setattr(notification_delivery, "email_is_configured", lambda: True)

        async def enabled_orgs(*, require_weekly=False):
            return set() if require_weekly else {org}

        monkeypatch.setattr(
            notification_delivery, "_email_enabled_org_ids", enabled_orgs
        )
        sent: list[dict] = []
        monkeypatch.setattr(
            notification_delivery,
            "_provider_send_email",
            lambda **kwargs: sent.append(kwargs) or "automatic-message-1",
        )
        await notifications.emit(
            organization_id=org, type="security", title="automatic"
        )

        outcome = await notification_delivery.run_delivery_cycle(
            max_orgs=1, per_org_limit=10
        )
        assert outcome == {
            "organizations": 1,
            "delivered": 1,
            "retried": 0,
            "dead_letter": 0,
        }
        assert sent[0]["subject"] == "automatic"
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_weekly_digest_receipt_is_idempotent_and_automatically_delivered(
    monkeypatch,
):
    org = f"orgD8-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        member_id = await _insert_member(org, "user")
        await _set_notification_settings(org, {"email": True, "weekly_digest": True})
        notification_id = await notifications.emit(
            organization_id=org, type="task_failure", title="failed last week"
        )
        notification_rows = await reflect_table("notifications")
        now = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)  # Monday
        async with engine.begin() as conn:
            await conn.execute(
                update(notification_rows)
                .where(notification_rows.c.id == notification_id)
                .values(created_at=now - timedelta(days=2))
            )

        first = await notification_delivery.materialize_weekly_digests(now=now)
        second = await notification_delivery.materialize_weekly_digests(now=now)
        assert first == 1
        assert second == 0

        sent: list[dict] = []
        monkeypatch.setattr(notification_delivery, "email_is_configured", lambda: True)
        monkeypatch.setattr(
            notification_delivery,
            "_provider_send_email",
            lambda **kwargs: sent.append(kwargs) or "digest-message-1",
        )
        outcome = await notification_delivery.run_weekly_digest_cycle(now=now)
        assert outcome["materialized"] == 0
        assert outcome["delivered"] == 1
        assert sent[0]["to"].startswith(member_id)
        assert "Task Failure: 1" in sent[0]["body"]

        delivery = await reflect_table("notification_delivery_receipts")
        async with engine.begin() as conn:
            receipt = (
                (
                    await conn.execute(
                        select(delivery).where(
                            delivery.c.organization_id == org,
                            delivery.c.delivery_kind == "weekly_digest",
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert receipt["status"] == "delivered"
        assert receipt["period_start"] == datetime(2026, 7, 6, tzinfo=timezone.utc)
        assert receipt["period_end"] == datetime(2026, 7, 13, tzinfo=timezone.utc)
    finally:
        await _cleanup([org])


def test_automatic_notification_jobs_are_registered():
    import main
    from jobs.notification_delivery import scheduler

    assert {job.id for job in scheduler.get_jobs()} == {
        "notification-email-delivery",
        "notification-weekly-digest",
    }
    assert scheduler in main._SCHEDULERS
