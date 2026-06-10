from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from core import permissions
from core.auth import get_current_member
from core.models import Member
from skills.registry import get_skill_db, list_skills_db

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("")
@router.get("/")
async def list_skills(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_skills", member.organization_id)
    return await list_skills_db(member.organization_id)


@router.get("/{slug}")
async def get_skill(slug: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "view_skill", slug)
    skill = await get_skill_db(member.organization_id, slug)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill
