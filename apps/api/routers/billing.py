"""Billing endpoints (W4.2 / W4.3)."""

from __future__ import annotations

import dataclasses
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select as _select

from core import billing, billing_usage
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from core.plans import get_entitlements
from core.settings_store import require_admin

router = APIRouter(tags=["billing"])


class CheckoutRequest(BaseModel):
    plan: Literal["pro", "enterprise"]


@router.post("/settings/billing/checkout")
async def billing_checkout(
    req: CheckoutRequest,
    member: Member = Depends(get_current_member),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=255,
    ),
) -> dict:
    require_admin(member)
    try:
        url = await billing.create_checkout(
            member.organization_id,
            req.plan,
            idempotency_key=idempotency_key,
        )
    except billing.BillingConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except billing.BillingInvalidPlan as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except billing.BillingNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except billing.BillingProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"checkout_url": url}


@router.post("/settings/billing/portal")
async def billing_portal(
    member: Member = Depends(get_current_member),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=255,
    ),
) -> dict:
    require_admin(member)
    try:
        url = await billing.create_portal(
            member.organization_id,
            idempotency_key=idempotency_key,
        )
    except billing.BillingAccountNotFound as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except billing.BillingNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except billing.BillingProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"portal_url": url}


@router.get("/settings/billing/usage")
async def billing_usage_endpoint(member: Member = Depends(get_current_member)) -> dict:
    """Return the current month's accumulated usage for the member's org.

    Includes plan entitlements and a best-effort over_budget flag.
    Accessible to any org member (no admin gate — it's their own org).
    """
    usage = await billing_usage.monthly_usage(member.organization_id)

    # Resolve the org's plan for entitlements and overage signal.
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        plan = (
            await conn.execute(
                _select(orgs.c.plan).where(orgs.c.id == member.organization_id)
            )
        ).scalar_one_or_none() or "trial"

    ent = get_entitlements(plan)
    ent_d = dataclasses.asdict(ent)
    ent_d["features"] = sorted(ent.features)

    # over_budget: monthly cost vs monthly equivalent of the daily limit.
    # daily_cost_limit_usd == 0 means unlimited (enterprise).
    monthly_cap = (ent.daily_cost_limit_usd * 30) if ent.daily_cost_limit_usd else 0.0
    over = bool(monthly_cap and usage["cost_usd"] > monthly_cap)

    return {**usage, "plan": plan, "entitlements": ent_d, "over_budget": over}


@router.post("/billing/webhook")
async def billing_webhook(request: Request) -> dict:
    if not billing.is_configured() or not settings.stripe_webhook_secret:
        raise HTTPException(status_code=404, detail="Billing webhook is not configured")
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        return await billing.handle_webhook(payload, signature)
    except billing.BillingWebhookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except billing.BillingNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
