"""Production Stripe billing integration.

Stripe owns payment details and subscription collection. Chronos keeps only the
minimum tenant-scoped reconciliation state needed to open the customer portal,
apply plan entitlements, and make webhook delivery idempotent.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import Any

import stripe
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from core import audit
from core.audit_redaction import redact
from core.config import settings
from core.db import engine, reflect_table
from core.plans import get_entitlements


class BillingError(Exception):
    """Base class for safe, user-presentable billing failures."""


class BillingNotConfigured(BillingError):
    pass


class BillingInvalidPlan(BillingError):
    pass


class BillingProviderError(BillingError):
    pass


class BillingAccountNotFound(BillingError):
    pass


class BillingConflict(BillingError):
    pass


class BillingWebhookError(BillingError):
    pass


_ORG_METADATA_KEY = "chronos_org_id"
_PRICE_METADATA_KEY = "chronos_price_id"
_SUPPORTED_WEBHOOK_TYPES = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
}
_ENTITLED_SUBSCRIPTION_STATUSES = {"active", "trialing", "past_due"}
_TERMINAL_SUBSCRIPTION_STATUSES = {
    "canceled",
    "incomplete",
    "incomplete_expired",
    "paused",
    "unpaid",
}


def is_configured() -> bool:
    """Return true only for a complete, internally consistent Stripe setup."""
    values = (
        settings.stripe_secret_key,
        settings.stripe_webhook_secret,
        settings.stripe_price_pro,
        settings.stripe_price_enterprise,
    )
    return bool(all(value.strip() for value in values)) and (
        settings.stripe_price_pro != settings.stripe_price_enterprise
    )


def _require_configuration() -> None:
    if is_configured():
        return
    missing = [
        name
        for name, value in (
            ("STRIPE_SECRET_KEY", settings.stripe_secret_key),
            ("STRIPE_WEBHOOK_SECRET", settings.stripe_webhook_secret),
            ("STRIPE_PRICE_PRO", settings.stripe_price_pro),
            ("STRIPE_PRICE_ENTERPRISE", settings.stripe_price_enterprise),
        )
        if not value.strip()
    ]
    if missing:
        raise BillingNotConfigured(
            "Stripe billing is incomplete; missing " + ", ".join(missing)
        )
    raise BillingNotConfigured("Stripe Pro and Enterprise price IDs must be distinct")


def _price_for_plan(plan: str) -> str:
    return {
        "pro": settings.stripe_price_pro,
        "enterprise": settings.stripe_price_enterprise,
    }.get(plan, "")


def _plan_for_price(price: str | None) -> str | None:
    if not price:
        return None
    matches = [
        plan
        for plan in ("pro", "enterprise")
        if _price_for_plan(plan) and _price_for_plan(plan) == price
    ]
    return matches[0] if len(matches) == 1 else None


def _idempotency_key(
    kind: str,
    org_id: str,
    scope: str,
    request_key: str | None = None,
    *,
    bucket_seconds: int = 300,
) -> str:
    """Create a tenant-bound Stripe idempotency key without leaking tenant IDs.

    A caller-provided key makes HTTP retries exact. The short server-side time
    bucket is the fallback for existing clients that do not yet send the header.
    Customer creation uses ``bucket_seconds=0`` for a stable key.
    """
    token = (request_key or "").strip()
    if not token:
        token = (
            "stable" if bucket_seconds == 0 else str(int(time.time() // bucket_seconds))
        )
    digest = hashlib.sha256(f"{kind}:{org_id}:{scope}:{token}".encode()).hexdigest()
    return f"chronos-{kind}-{digest}"


def _stripe_client() -> stripe.StripeClient:
    if not settings.stripe_secret_key:
        raise BillingNotConfigured("STRIPE_SECRET_KEY is not configured")
    return stripe.StripeClient(
        settings.stripe_secret_key,
        max_network_retries=2,
        http_client=stripe.RequestsClient(timeout=15.0),
    )


def _object_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        raw = value.get("id")
        return str(raw) if raw else None
    raw = getattr(value, "id", None)
    return str(raw) if raw else None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    converter = getattr(value, "to_dict_recursive", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, dict):
            return converted
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _provider_create_customer(*, org_id: str, name: str, idempotency_key: str) -> str:
    try:
        customer = _stripe_client().v1.customers.create(
            {
                "name": name,
                "description": f"Chronos organization {name}",
                "metadata": {_ORG_METADATA_KEY: org_id},
            },
            {"idempotency_key": idempotency_key},
        )
    except stripe.StripeError as exc:
        raise BillingProviderError(
            "Stripe could not create the billing customer"
        ) from exc
    customer_id = _object_id(customer)
    if not customer_id:
        raise BillingProviderError("Stripe returned a customer without an ID")
    return customer_id


def _provider_create_checkout(
    *,
    org_id: str,
    plan: str,
    price: str,
    customer_id: str,
    idempotency_key: str,
) -> str:
    metadata = {_ORG_METADATA_KEY: org_id, _PRICE_METADATA_KEY: price}
    base_url = settings.frontend_base_url.rstrip("/")
    try:
        session = _stripe_client().v1.checkout.sessions.create(
            {
                "mode": "subscription",
                "customer": customer_id,
                "client_reference_id": org_id,
                "line_items": [{"price": price, "quantity": 1}],
                "success_url": (
                    f"{base_url}/settings/billing?checkout=success"
                    "&session_id={CHECKOUT_SESSION_ID}"
                ),
                "cancel_url": f"{base_url}/settings/billing?checkout=cancelled",
                "metadata": metadata,
                "subscription_data": {"metadata": metadata},
            },
            {"idempotency_key": idempotency_key},
        )
    except stripe.StripeError as exc:
        raise BillingProviderError(
            "Stripe could not create the checkout session"
        ) from exc
    url = getattr(session, "url", None)
    if not url:
        raise BillingProviderError("Stripe returned a checkout session without a URL")
    return str(url)


def _provider_create_portal(
    *, org_id: str, customer_id: str, idempotency_key: str
) -> str:
    del org_id  # Tenant binding is enforced by the persisted customer lookup.
    try:
        session = _stripe_client().v1.billing_portal.sessions.create(
            {
                "customer": customer_id,
                "return_url": f"{settings.frontend_base_url.rstrip('/')}/settings/billing",
            },
            {"idempotency_key": idempotency_key},
        )
    except stripe.StripeError as exc:
        raise BillingProviderError(
            "Stripe could not create the customer portal session"
        ) from exc
    url = getattr(session, "url", None)
    if not url:
        raise BillingProviderError("Stripe returned a portal session without a URL")
    return str(url)


def _subscription_price(obj: dict[str, Any]) -> str | None:
    items = _as_dict(obj.get("items"))
    for item_value in items.get("data") or []:
        item = _as_dict(item_value)
        price_id = _object_id(item.get("price"))
        if _plan_for_price(price_id):
            return price_id
    return None


def _parse_event(payload: bytes, signature: str) -> dict[str, Any]:
    """Verify a raw Stripe webhook and normalize only trusted billing fields.

    The organization is read exclusively from metadata that Chronos bound when
    creating the Customer/Checkout/Subscription. Plans are always derived from
    the configured Stripe Price IDs; a payload ``plan`` field is never trusted.
    """
    if not settings.stripe_webhook_secret:
        raise BillingNotConfigured("STRIPE_WEBHOOK_SECRET is not configured")
    try:
        raw_event = stripe.Webhook.construct_event(
            payload,
            signature,
            settings.stripe_webhook_secret,
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise BillingWebhookError(
            "Invalid Stripe webhook signature or payload"
        ) from exc

    event = _as_dict(raw_event)
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    event_created = event.get("created")
    if not event_id or not event_type or not isinstance(event_created, int):
        raise BillingWebhookError("Stripe webhook is missing required event fields")

    normalized: dict[str, Any] = {
        "event_id": event_id,
        "event_created": event_created,
        "type": event_type,
    }
    if event_type not in _SUPPORTED_WEBHOOK_TYPES:
        return normalized

    data = _as_dict(event.get("data"))
    obj = _as_dict(data.get("object"))
    metadata = _as_dict(obj.get("metadata"))
    org_id = str(metadata.get(_ORG_METADATA_KEY) or "")
    if not org_id or len(org_id) > 200:
        raise BillingWebhookError(
            "Stripe billing event has no valid Chronos organization"
        )

    normalized.update(
        {
            "org_id": org_id,
            "customer_id": _object_id(obj.get("customer")),
            "subscription_id": (
                _object_id(obj.get("subscription"))
                if event_type == "checkout.session.completed"
                else _object_id(obj)
            ),
        }
    )

    if event_type == "checkout.session.completed":
        if obj.get("mode") != "subscription" or obj.get("payment_status") not in {
            "paid",
            "no_payment_required",
        }:
            raise BillingWebhookError("Stripe checkout is not a paid subscription")
        price_id = str(metadata.get(_PRICE_METADATA_KEY) or "")
        plan = _plan_for_price(price_id)
        if not plan:
            raise BillingWebhookError("Stripe checkout references an unknown price")
        normalized.update({"plan": plan, "subscription_status": "active"})
        return normalized

    status = str(obj.get("status") or "")
    normalized["subscription_status"] = status or (
        "canceled" if event_type == "customer.subscription.deleted" else "unknown"
    )
    normalized["current_period_end"] = obj.get("current_period_end")

    if event_type != "customer.subscription.deleted":
        price_id = _subscription_price(obj)
        plan = _plan_for_price(price_id)
        if not plan:
            raise BillingWebhookError("Stripe subscription references an unknown price")
        normalized["plan"] = plan
    return normalized


async def set_org_plan(org_id: str, plan: str, *, actor_id: str = "system") -> None:
    """Set ``organizations.plan`` (validated against known plans) and audit."""
    plan = get_entitlements(plan).plan
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(
            update(organizations).where(organizations.c.id == org_id).values(plan=plan)
        )
    await audit.log(
        "billing_plan_changed",
        actor_id,
        "billing.set_plan",
        organization_id=org_id,
        resource_type="organization",
        resource_id=org_id,
        payload={"plan": plan},
    )


async def _billing_profile(org_id: str) -> tuple[str, str | None, str | None]:
    organizations = await reflect_table("organizations")
    accounts = await reflect_table("billing_accounts")
    async with engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    select(
                        organizations.c.name,
                        accounts.c.stripe_customer_id,
                        accounts.c.subscription_status,
                    )
                    .select_from(
                        organizations.outerjoin(
                            accounts,
                            accounts.c.organization_id == organizations.c.id,
                        )
                    )
                    .where(organizations.c.id == org_id)
                )
            )
            .mappings()
            .first()
        )
    if not row:
        raise BillingProviderError("The organization no longer exists")
    return str(row["name"]), row["stripe_customer_id"], row["subscription_status"]


async def _persist_customer(org_id: str, customer_id: str) -> str:
    accounts = await reflect_table("billing_accounts")
    stmt = (
        pg_insert(accounts)
        .values(
            organization_id=org_id,
            region=settings.region,
            stripe_customer_id=customer_id,
            plan="trial",
        )
        .on_conflict_do_update(
            index_elements=["organization_id"],
            set_={
                "stripe_customer_id": func.coalesce(
                    accounts.c.stripe_customer_id,
                    customer_id,
                ),
                "updated_at": datetime.now(timezone.utc),
            },
        )
        .returning(accounts.c.stripe_customer_id)
    )
    try:
        async with engine.begin() as conn:
            customer_owner = await conn.scalar(
                select(accounts.c.organization_id).where(
                    accounts.c.stripe_customer_id == customer_id
                )
            )
            if customer_owner and customer_owner != org_id:
                raise BillingProviderError(
                    "Stripe returned a customer already bound to another organization"
                )
            persisted = await conn.scalar(stmt)
    except IntegrityError as exc:
        raise BillingProviderError(
            "The Stripe customer mapping conflicted with another billing account"
        ) from exc
    if not persisted:
        raise BillingProviderError("The Stripe customer mapping could not be persisted")
    return str(persisted)


async def _ensure_customer(org_id: str) -> str:
    name, customer_id, _ = await _billing_profile(org_id)
    if customer_id:
        return str(customer_id)
    created = await asyncio.to_thread(
        _provider_create_customer,
        org_id=org_id,
        name=name,
        idempotency_key=_idempotency_key("customer", org_id, "v1", bucket_seconds=0),
    )
    return await _persist_customer(org_id, created)


async def create_checkout(
    org_id: str, plan: str, *, idempotency_key: str | None = None
) -> str:
    _require_configuration()
    normalized_plan = plan.strip().lower()
    price = _price_for_plan(normalized_plan)
    if not price:
        raise BillingInvalidPlan(f"Plan '{plan}' is not purchasable")
    _, _, subscription_status = await _billing_profile(org_id)
    if subscription_status in _ENTITLED_SUBSCRIPTION_STATUSES:
        raise BillingConflict(
            "This organization already has an active subscription; use the billing portal"
        )
    customer_id = await _ensure_customer(org_id)
    return await asyncio.to_thread(
        _provider_create_checkout,
        org_id=org_id,
        plan=normalized_plan,
        price=price,
        customer_id=customer_id,
        idempotency_key=_idempotency_key(
            "checkout", org_id, normalized_plan, idempotency_key
        ),
    )


async def create_portal(org_id: str, *, idempotency_key: str | None = None) -> str:
    _require_configuration()
    _, customer_id, _ = await _billing_profile(org_id)
    if not customer_id:
        raise BillingAccountNotFound(
            "No Stripe customer exists for this organization; start checkout first"
        )
    return await asyncio.to_thread(
        _provider_create_portal,
        org_id=org_id,
        customer_id=str(customer_id),
        idempotency_key=_idempotency_key("portal", org_id, "manage", idempotency_key),
    )


def _period_end(value: Any) -> datetime | None:
    if isinstance(value, int) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


async def _apply_webhook_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event["type"])
    if event_type not in _SUPPORTED_WEBHOOK_TYPES:
        return {"handled": False, "event_type": event_type}

    org_id = str(event.get("org_id") or "")
    event_id = str(event.get("event_id") or "")
    event_created = int(event.get("event_created") or 0)
    customer_id = str(event.get("customer_id") or "")
    subscription_id = str(event.get("subscription_id") or "")
    status = str(event.get("subscription_status") or "unknown")
    if not all((org_id, event_id, event_created, customer_id)):
        raise BillingWebhookError(
            "Stripe billing event is missing reconciliation fields"
        )

    organizations = await reflect_table("organizations")
    accounts = await reflect_table("billing_accounts")
    events = await reflect_table("billing_webhook_events")
    audit_log = await reflect_table("audit_log")

    async with engine.begin() as conn:
        organization_exists = await conn.scalar(
            select(organizations.c.id).where(organizations.c.id == org_id)
        )
        if not organization_exists:
            raise BillingWebhookError(
                "Stripe billing event references an unknown organization"
            )

        account = (
            (
                await conn.execute(
                    select(accounts)
                    .where(accounts.c.organization_id == org_id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        customer_owner = await conn.scalar(
            select(accounts.c.organization_id).where(
                accounts.c.stripe_customer_id == customer_id
            )
        )
        if customer_owner and customer_owner != org_id:
            raise BillingWebhookError(
                "Stripe customer is bound to another organization"
            )
        if account and account["stripe_customer_id"] not in {None, customer_id}:
            raise BillingWebhookError("Stripe customer does not match the organization")
        if subscription_id:
            subscription_owner = await conn.scalar(
                select(accounts.c.organization_id).where(
                    accounts.c.stripe_subscription_id == subscription_id
                )
            )
            if subscription_owner and subscription_owner != org_id:
                raise BillingWebhookError(
                    "Stripe subscription is bound to another organization"
                )

        inserted_event = await conn.scalar(
            pg_insert(events)
            .values(
                stripe_event_id=event_id,
                organization_id=org_id,
                event_type=event_type,
                event_created=event_created,
            )
            .on_conflict_do_nothing(index_elements=["stripe_event_id"])
            .returning(events.c.stripe_event_id)
        )
        if not inserted_event:
            recorded_org = await conn.scalar(
                select(events.c.organization_id).where(
                    events.c.stripe_event_id == event_id
                )
            )
            if recorded_org != org_id:
                raise BillingWebhookError(
                    "Stripe event is already bound to another organization"
                )
            return {
                "handled": True,
                "duplicate": True,
                "org_id": org_id,
                "event_type": event_type,
            }

        if (
            account
            and account["stripe_subscription_id"]
            and subscription_id
            and account["stripe_subscription_id"] != subscription_id
            and (
                event_type == "customer.subscription.deleted"
                or status in _TERMINAL_SUBSCRIPTION_STATUSES
            )
        ):
            return {
                "handled": True,
                "unrelated_subscription": True,
                "org_id": org_id,
                "event_type": event_type,
            }

        last_event_created = account["last_event_created"] if account else None
        if last_event_created is not None and event_created < last_event_created:
            return {
                "handled": True,
                "stale": True,
                "org_id": org_id,
                "event_type": event_type,
            }

        if event_type == "customer.subscription.deleted":
            applied_plan = "trial"
            status = "canceled"
        elif status in _ENTITLED_SUBSCRIPTION_STATUSES:
            applied_plan = str(event.get("plan") or "")
        elif status in _TERMINAL_SUBSCRIPTION_STATUSES:
            applied_plan = "trial"
        else:
            raise BillingWebhookError("Stripe subscription has an unsupported status")

        if applied_plan not in {"trial", "pro", "enterprise"}:
            raise BillingWebhookError(
                "Stripe billing event does not map to a Chronos plan"
            )

        values = {
            "stripe_customer_id": customer_id,
            "stripe_subscription_id": subscription_id or None,
            "subscription_status": status,
            "plan": applied_plan,
            "current_period_end": _period_end(event.get("current_period_end")),
            "last_event_created": event_created,
            "updated_at": datetime.now(timezone.utc),
        }
        if account:
            await conn.execute(
                update(accounts)
                .where(accounts.c.organization_id == org_id)
                .values(**values)
            )
        else:
            await conn.execute(
                insert(accounts).values(
                    organization_id=org_id,
                    region=settings.region,
                    **values,
                )
            )
        await conn.execute(
            update(organizations)
            .where(organizations.c.id == org_id)
            .values(plan=applied_plan)
        )
        await conn.execute(
            insert(audit_log).values(
                organization_id=org_id,
                region=settings.region,
                event_type="billing_plan_changed",
                actor_id="stripe_webhook",
                action="billing.set_plan",
                resource_type="organization",
                resource_id=org_id,
                payload=redact(
                    {
                        "plan": applied_plan,
                        "subscription_status": status,
                        "stripe_event_id": event_id,
                    }
                ),
            )
        )

    return {
        "handled": True,
        "org_id": org_id,
        "plan": applied_plan,
        "subscription_status": status,
        "event_type": event_type,
    }


async def handle_webhook(payload: bytes, signature: str) -> dict[str, Any]:
    event = _parse_event(payload, signature)
    return await _apply_webhook_event(event)
