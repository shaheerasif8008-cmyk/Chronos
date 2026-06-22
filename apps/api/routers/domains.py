"""Admin endpoints to verify domain ownership via DNS-TXT (W1 Phase 3)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import domains, permissions
from core.auth import get_current_member
from core.models import Member

router = APIRouter(prefix="/domains", tags=["domains"])


class DomainRequest(BaseModel):
    domain: str


@router.post("/verify/start")
async def verify_start(req: DomainRequest, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "manage_sso", member.organization_id)
    record = await domains.start_domain_verification(member.organization_id, req.domain)
    return {"record": record}


@router.post("/verify/check")
async def verify_check(req: DomainRequest, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "manage_sso", member.organization_id)
    verified = await domains.check_domain_verification(member.organization_id, req.domain)
    if not verified:
        raise HTTPException(status_code=400, detail="TXT record not found or does not match")
    return {"verified": True}
