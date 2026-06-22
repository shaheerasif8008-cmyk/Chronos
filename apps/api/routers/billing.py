"""Billing endpoints (W4.2)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core import billing
from core.auth import get_current_member
from core.config import settings
from core.models import Member
from core.settings_store import require_admin

router = APIRouter(tags=["billing"])


class CheckoutRequest(BaseModel):
    plan: str


@router.post("/settings/billing/checkout")
async def billing_checkout(req: CheckoutRequest, member: Member = Depends(get_current_member)) -> dict:
    require_admin(member)
    try:
        url = await billing.create_checkout(member.organization_id, req.plan)
    except billing.BillingNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"checkout_url": url}


@router.post("/settings/billing/portal")
async def billing_portal(member: Member = Depends(get_current_member)) -> dict:
    require_admin(member)
    try:
        url = await billing.create_portal(member.organization_id)
    except billing.BillingNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"portal_url": url}


@router.post("/billing/webhook")
async def billing_webhook(request: Request) -> dict:
    if not billing.is_configured() or not settings.stripe_webhook_secret:
        raise HTTPException(status_code=404, detail="Billing webhook is not configured")
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        return await billing.handle_webhook(payload, signature)
    except billing.BillingNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
