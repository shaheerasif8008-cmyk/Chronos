"""Productized compliance evidence export API."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core import compliance_export, permissions
from core.auth import get_current_member
from core.exceptions import PermissionDenied
from core.models import Member


router = APIRouter(prefix="/compliance", tags=["compliance"])


class CreateComplianceExport(BaseModel):
    since: datetime | None = None
    until: datetime | None = None
    categories: list[str] | None = Field(default=None, max_length=5)


@router.post("/exports")
async def create_export(
    body: CreateComplianceExport,
    member: Member = Depends(get_current_member),
) -> dict:
    try:
        await permissions.check(member, "export_compliance", member.organization_id)
        return await compliance_export.create_export(
            member,
            since=body.since,
            until=body.until,
            categories=body.categories,
        )
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail="Not authorized to export compliance evidence") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
