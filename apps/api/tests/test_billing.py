"""Production Stripe billing: tenant binding, idempotency, and signed events."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

import main
from core import billing
from core.auth import create_access_token
from core.config import Settings
from core.db import engine, reflect_table


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://test",
    )


async def _org_admin(plan: str = "trial") -> tuple[str, str, str]:
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    subdomain = f"o{org_id[:8]}"
    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(
            organizations.insert().values(
                id=org_id,
                slug=subdomain,
                subdomain=subdomain,
                name="Billing Test Org",
                plan=plan,
            )
        )
        await conn.execute(
            members.insert().values(
                id=member_id,
                organization_id=org_id,
                email=f"a{member_id[:8]}@t.io",
                role="admin",
            )
        )
    return org_id, create_access_token(member_id, org_id=org_id), subdomain


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(billing.settings, "stripe_secret_key", "sk_test_x")
    monkeypatch.setattr(billing.settings, "stripe_webhook_secret", "whsec_test_x")
    monkeypatch.setattr(billing.settings, "stripe_price_pro", "price_pro")
    monkeypatch.setattr(
        billing.settings,
        "stripe_price_enterprise",
        "price_enterprise",
    )


def _signed_event(
    event: dict[str, Any], secret: str = "whsec_test_x"
) -> tuple[bytes, str]:
    payload = json.dumps(event, separators=(",", ":")).encode()
    timestamp = int(time.time())
    digest = hmac.new(
        secret.encode(),
        f"{timestamp}.".encode() + payload,
        hashlib.sha256,
    ).hexdigest()
    return payload, f"t={timestamp},v1={digest}"


def _checkout_event(
    org_id: str,
    *,
    event_id: str | None = None,
    event_created: int | None = None,
    customer_id: str | None = None,
    price_id: str = "price_pro",
    raw_plan: str = "enterprise",
) -> dict[str, Any]:
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex}",
        "object": "event",
        "created": event_created or int(time.time()),
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": f"cs_{uuid.uuid4().hex}",
                "object": "checkout.session",
                "mode": "subscription",
                "payment_status": "paid",
                "customer": customer_id or f"cus_{uuid.uuid4().hex}",
                "subscription": f"sub_{uuid.uuid4().hex}",
                "metadata": {
                    "chronos_org_id": org_id,
                    "chronos_price_id": price_id,
                    # An attacker-controlled/raw plan field is deliberately ignored.
                    "plan": raw_plan,
                },
            }
        },
    }


def _subscription_event(
    org_id: str,
    *,
    event_type: str,
    event_created: int,
    customer_id: str,
    subscription_id: str,
    status: str,
    price_id: str = "price_enterprise",
) -> dict[str, Any]:
    return {
        "id": f"evt_{uuid.uuid4().hex}",
        "object": "event",
        "created": event_created,
        "type": event_type,
        "data": {
            "object": {
                "id": subscription_id,
                "object": "subscription",
                "customer": customer_id,
                "status": status,
                "current_period_end": int(time.time()) + 30 * 24 * 60 * 60,
                "metadata": {"chronos_org_id": org_id},
                "items": {"data": [{"price": {"id": price_id}}]},
            }
        },
    }


def test_is_configured_requires_complete_distinct_tuple(monkeypatch):
    monkeypatch.setattr(billing.settings, "stripe_secret_key", "")
    assert billing.is_configured() is False

    _configure(monkeypatch)
    assert billing.is_configured() is True

    monkeypatch.setattr(billing.settings, "stripe_price_enterprise", "price_pro")
    assert billing.is_configured() is False


def test_partial_stripe_settings_are_rejected():
    with pytest.raises(ValidationError, match="all four values together"):
        Settings(
            aws_s3_bucket="test-bucket",
            stripe_secret_key="sk_test_partial",
            stripe_webhook_secret="",
            stripe_price_pro="",
            stripe_price_enterprise="",
        )


@pytest.mark.asyncio
async def test_set_org_plan_updates_and_audits():
    org_id, _, _ = await _org_admin("trial")
    await billing.set_org_plan(org_id, "pro")
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        plan = await conn.scalar(
            select(organizations.c.plan).where(organizations.c.id == org_id)
        )
    assert plan == "pro"


@pytest.mark.asyncio
async def test_checkout_degraded_when_unconfigured(monkeypatch):
    monkeypatch.setattr(billing.settings, "stripe_secret_key", "")
    _, token, subdomain = await _org_admin("trial")
    async with _client() as client:
        response = await client.post(
            "/settings/billing/checkout",
            json={"plan": "pro"},
            headers={
                "Authorization": f"Bearer {token}",
                "X-Chronos-Org": subdomain,
            },
        )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_checkout_rejects_unknown_plan_before_provider_call(monkeypatch):
    _configure(monkeypatch)
    _, token, subdomain = await _org_admin("trial")
    async with _client() as client:
        response = await client.post(
            "/settings/billing/checkout",
            json={"plan": "attacker-price"},
            headers={
                "Authorization": f"Bearer {token}",
                "X-Chronos-Org": subdomain,
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_checkout_persists_customer_and_reuses_idempotency_key(monkeypatch):
    _configure(monkeypatch)
    created_customers: list[dict[str, Any]] = []
    checkout_calls: list[dict[str, Any]] = []
    customer_id = f"cus_{uuid.uuid4().hex}"

    def create_customer(**kwargs):
        created_customers.append(kwargs)
        return customer_id

    def create_checkout(**kwargs):
        checkout_calls.append(kwargs)
        return "https://checkout.stripe.test/session"

    monkeypatch.setattr(billing, "_provider_create_customer", create_customer)
    monkeypatch.setattr(billing, "_provider_create_checkout", create_checkout)
    org_id, token, subdomain = await _org_admin("trial")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Chronos-Org": subdomain,
        "Idempotency-Key": "upgrade-click-1",
    }
    async with _client() as client:
        first = await client.post(
            "/settings/billing/checkout",
            json={"plan": "pro"},
            headers=headers,
        )
        second = await client.post(
            "/settings/billing/checkout",
            json={"plan": "pro"},
            headers=headers,
        )

    assert first.status_code == second.status_code == 200
    assert len(created_customers) == 1
    assert created_customers[0]["org_id"] == org_id
    assert [call["idempotency_key"] for call in checkout_calls] == [
        checkout_calls[0]["idempotency_key"],
        checkout_calls[0]["idempotency_key"],
    ]
    assert checkout_calls[0]["customer_id"] == customer_id
    assert checkout_calls[0]["price"] == "price_pro"

    accounts = await reflect_table("billing_accounts")
    async with engine.begin() as conn:
        persisted = await conn.scalar(
            select(accounts.c.stripe_customer_id).where(
                accounts.c.organization_id == org_id
            )
        )
    assert persisted == customer_id


@pytest.mark.asyncio
async def test_portal_uses_only_the_authenticated_org_customer(monkeypatch):
    _configure(monkeypatch)
    portal_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        billing,
        "_provider_create_portal",
        lambda **kwargs: (
            portal_calls.append(kwargs) or "https://billing.stripe.test/session"
        ),
    )
    org_id, token, subdomain = await _org_admin("pro")
    customer_id = f"cus_{uuid.uuid4().hex}"
    accounts = await reflect_table("billing_accounts")
    async with engine.begin() as conn:
        await conn.execute(
            accounts.insert().values(
                organization_id=org_id,
                stripe_customer_id=customer_id,
                plan="pro",
            )
        )

    async with _client() as client:
        response = await client.post(
            "/settings/billing/portal",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Chronos-Org": subdomain,
                "Idempotency-Key": "portal-open-1",
            },
        )
        replay = await client.post(
            "/settings/billing/portal",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Chronos-Org": subdomain,
                "Idempotency-Key": "portal-open-1",
            },
        )
    assert response.status_code == replay.status_code == 200
    assert portal_calls[0]["org_id"] == org_id
    assert portal_calls[0]["customer_id"] == customer_id
    assert "portal-open-1" not in portal_calls[0]["idempotency_key"]
    assert portal_calls[1]["idempotency_key"] == portal_calls[0]["idempotency_key"]


@pytest.mark.asyncio
async def test_portal_requires_an_existing_tenant_customer(monkeypatch):
    _configure(monkeypatch)
    _, token, subdomain = await _org_admin("trial")
    async with _client() as client:
        response = await client.post(
            "/settings/billing/portal",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Chronos-Org": subdomain,
            },
        )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_signed_checkout_webhook_maps_price_and_is_exactly_once(monkeypatch):
    _configure(monkeypatch)
    org_id, _, _ = await _org_admin("trial")
    event = _checkout_event(org_id, raw_plan="enterprise")
    payload, signature = _signed_event(event)
    headers = {"stripe-signature": signature}

    async with _client() as client:
        first = await client.post("/billing/webhook", content=payload, headers=headers)
        replay = await client.post("/billing/webhook", content=payload, headers=headers)

    assert first.status_code == replay.status_code == 200
    assert first.json()["plan"] == "pro"
    assert replay.json()["duplicate"] is True

    organizations = await reflect_table("organizations")
    accounts = await reflect_table("billing_accounts")
    events = await reflect_table("billing_webhook_events")
    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        assert (
            await conn.scalar(
                select(organizations.c.plan).where(organizations.c.id == org_id)
            )
            == "pro"
        )
        account = (
            (
                await conn.execute(
                    select(accounts).where(accounts.c.organization_id == org_id)
                )
            )
            .mappings()
            .one()
        )
        assert account["stripe_customer_id"] == event["data"]["object"]["customer"]
        assert account["subscription_status"] == "active"
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(events)
                .where(events.c.stripe_event_id == event["id"])
            )
            == 1
        )
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(audit_log)
                .where(
                    audit_log.c.organization_id == org_id,
                    audit_log.c.actor_id == "stripe_webhook",
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_subscription_update_and_deletion_reconcile_entitlements(monkeypatch):
    _configure(monkeypatch)
    org_id, _, _ = await _org_admin("trial")
    customer_id = f"cus_{uuid.uuid4().hex}"
    subscription_id = f"sub_{uuid.uuid4().hex}"
    now = int(time.time())
    activated = _subscription_event(
        org_id,
        event_type="customer.subscription.updated",
        event_created=now,
        customer_id=customer_id,
        subscription_id=subscription_id,
        status="active",
    )
    canceled = _subscription_event(
        org_id,
        event_type="customer.subscription.deleted",
        event_created=now + 1,
        customer_id=customer_id,
        subscription_id=subscription_id,
        status="canceled",
    )
    activated_payload, activated_signature = _signed_event(activated)
    canceled_payload, canceled_signature = _signed_event(canceled)

    async with _client() as client:
        active_response = await client.post(
            "/billing/webhook",
            content=activated_payload,
            headers={"stripe-signature": activated_signature},
        )
        canceled_response = await client.post(
            "/billing/webhook",
            content=canceled_payload,
            headers={"stripe-signature": canceled_signature},
        )

    assert active_response.status_code == canceled_response.status_code == 200
    assert active_response.json()["plan"] == "enterprise"
    assert canceled_response.json()["plan"] == "trial"
    accounts = await reflect_table("billing_accounts")
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        account = (
            (
                await conn.execute(
                    select(accounts).where(accounts.c.organization_id == org_id)
                )
            )
            .mappings()
            .one()
        )
        assert account["stripe_subscription_id"] == subscription_id
        assert account["subscription_status"] == "canceled"
        assert account["current_period_end"] is not None
        assert (
            await conn.scalar(
                select(organizations.c.plan).where(organizations.c.id == org_id)
            )
            == "trial"
        )


@pytest.mark.asyncio
async def test_invalid_signature_and_unknown_price_never_change_plan(monkeypatch):
    _configure(monkeypatch)
    org_id, _, _ = await _org_admin("trial")
    unknown_price, valid_signature = _signed_event(
        _checkout_event(org_id, price_id="price_attacker")
    )
    valid_payload, _ = _signed_event(_checkout_event(org_id))

    async with _client() as client:
        invalid_signature = await client.post(
            "/billing/webhook",
            content=valid_payload,
            headers={"stripe-signature": "t=1,v1=invalid"},
        )
        unknown = await client.post(
            "/billing/webhook",
            content=unknown_price,
            headers={"stripe-signature": valid_signature},
        )

    assert invalid_signature.status_code == 400
    assert unknown.status_code == 400
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        assert (
            await conn.scalar(
                select(organizations.c.plan).where(organizations.c.id == org_id)
            )
            == "trial"
        )


@pytest.mark.asyncio
async def test_older_subscription_event_is_recorded_but_not_applied(monkeypatch):
    _configure(monkeypatch)
    org_id, _, _ = await _org_admin("trial")
    customer_id = f"cus_{uuid.uuid4().hex}"
    newer = _checkout_event(
        org_id,
        event_created=int(time.time()),
        customer_id=customer_id,
    )
    older = _checkout_event(
        org_id,
        event_created=newer["created"] - 60,
        event_id=f"evt_{uuid.uuid4().hex}",
        price_id="price_enterprise",
        customer_id=customer_id,
    )
    newer_payload, newer_signature = _signed_event(newer)
    older_payload, older_signature = _signed_event(older)

    async with _client() as client:
        first = await client.post(
            "/billing/webhook",
            content=newer_payload,
            headers={"stripe-signature": newer_signature},
        )
        stale = await client.post(
            "/billing/webhook",
            content=older_payload,
            headers={"stripe-signature": older_signature},
        )

    assert first.status_code == stale.status_code == 200, (first.text, stale.text)
    assert stale.json()["stale"] is True
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        assert (
            await conn.scalar(
                select(organizations.c.plan).where(organizations.c.id == org_id)
            )
            == "pro"
        )


@pytest.mark.asyncio
async def test_webhook_rejects_customer_rebinding_across_tenants(monkeypatch):
    _configure(monkeypatch)
    owner_org, _, _ = await _org_admin("pro")
    target_org, _, _ = await _org_admin("trial")
    customer_id = f"cus_{uuid.uuid4().hex}"
    accounts = await reflect_table("billing_accounts")
    async with engine.begin() as conn:
        await conn.execute(
            accounts.insert().values(
                organization_id=owner_org,
                stripe_customer_id=customer_id,
                plan="pro",
            )
        )
    payload, signature = _signed_event(
        _checkout_event(target_org, customer_id=customer_id)
    )

    async with _client() as client:
        response = await client.post(
            "/billing/webhook",
            content=payload,
            headers={"stripe-signature": signature},
        )
    assert response.status_code == 400


def test_provider_checkout_binds_only_server_owned_metadata(monkeypatch):
    _configure(monkeypatch)
    captured: dict[str, Any] = {}

    class Sessions:
        def create(self, params, options):
            captured["params"] = params
            captured["options"] = options
            return type("Session", (), {"url": "https://checkout.stripe.test/s"})()

    fake_client = type(
        "Client",
        (),
        {
            "v1": type(
                "V1", (), {"checkout": type("Checkout", (), {"sessions": Sessions()})()}
            )()
        },
    )()
    monkeypatch.setattr(billing, "_stripe_client", lambda: fake_client)

    url = billing._provider_create_checkout(
        org_id="org-server-owned",
        plan="pro",
        price="price_pro",
        customer_id="cus_server_owned",
        idempotency_key="idem_hashed",
    )

    assert url == "https://checkout.stripe.test/s"
    assert captured["params"]["customer"] == "cus_server_owned"
    assert captured["params"]["metadata"] == {
        "chronos_org_id": "org-server-owned",
        "chronos_price_id": "price_pro",
    }
    assert (
        captured["params"]["subscription_data"]["metadata"]
        == captured["params"]["metadata"]
    )
    assert captured["options"] == {"idempotency_key": "idem_hashed"}
