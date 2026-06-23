"""Billing provider seam (Stripe). Truthful-degraded when unconfigured. All
provider-specific calls are isolated in _provider_* / _parse_event so the rest is
testable without the Stripe SDK or a live account."""
from __future__ import annotations

from sqlalchemy import update

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.plans import get_entitlements


class BillingNotConfigured(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.stripe_secret_key)


def _price_for_plan(plan: str) -> str:
    return {"pro": settings.stripe_price_pro, "enterprise": settings.stripe_price_enterprise}.get(plan, "")


async def set_org_plan(org_id: str, plan: str, *, actor_id: str = "system") -> None:
    """Set organizations.plan (validated against known plans) and audit."""
    plan = get_entitlements(plan).plan  # normalize/validate → falls back to trial if unknown
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(update(organizations).where(organizations.c.id == org_id).values(plan=plan))
    await audit.log("billing_plan_changed", actor_id, "billing.set_plan",
                    organization_id=org_id, resource_type="organization", resource_id=org_id,
                    payload={"plan": plan})


# ── Provider seams (monkeypatched in tests; real Stripe behind these) ──────────
# SECURITY CONTRACT for the real Stripe wiring (handle_webhook applies a plan to
# whatever org_id/plan the event names): a valid signature only proves the event
# came from our Stripe account — NOT that the event's org_id/plan are legitimate.
# Therefore the real impls MUST:
#   - `_provider_create_checkout`: bind org_id into server-side Checkout Session
#     metadata (never a client-settable field), and set price from the trusted
#     plan→price map. org_id here already comes from the authenticated admin's
#     `member.organization_id`, not client input — keep it that way.
#   - `_parse_event`: verify the Stripe signature FIRST (raise/return None on
#     failure → no plan change), then derive org_id ONLY from the session's
#     server-bound metadata (or a customer→org table) and plan ONLY from the
#     Stripe price→plan map. Never trust an org_id/plan from a raw payload field.
def _provider_create_checkout(*, org_id: str, plan: str, price: str) -> str:
    """Create a Stripe Checkout Session and return its URL. Real impl calls Stripe;
    raises if the SDK/secret isn't available. Tests monkeypatch this."""
    raise BillingNotConfigured("Stripe SDK not wired in this build")


def _provider_create_portal(*, org_id: str) -> str:
    raise BillingNotConfigured("Stripe SDK not wired in this build")


def _parse_event(payload: bytes, signature: str) -> dict | None:
    """Verify the webhook signature and return a normalized event
    {type, org_id, plan} or None (see the SECURITY CONTRACT above — org_id/plan
    must be derived from server-bound metadata + the price map, not the payload).
    Tests monkeypatch this."""
    raise BillingNotConfigured("Stripe SDK not wired in this build")


async def create_checkout(org_id: str, plan: str) -> str:
    if not is_configured():
        raise BillingNotConfigured("No billing provider is configured")
    price = _price_for_plan(plan)
    if not price:
        raise BillingNotConfigured(f"No price configured for plan '{plan}'")
    return _provider_create_checkout(org_id=org_id, plan=plan, price=price)


async def create_portal(org_id: str) -> str:
    if not is_configured():
        raise BillingNotConfigured("No billing provider is configured")
    return _provider_create_portal(org_id=org_id)


async def handle_webhook(payload: bytes, signature: str) -> dict:
    event = _parse_event(payload, signature)
    if not event:
        return {"handled": False}
    if (
        event.get("type") in {"checkout.session.completed", "customer.subscription.updated"}
        and event.get("org_id")
        and event.get("plan")
    ):
        await set_org_plan(event["org_id"], event["plan"], actor_id="stripe_webhook")
        return {"handled": True, "org_id": event["org_id"], "plan": event["plan"]}
    return {"handled": False}
