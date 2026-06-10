from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core import permissions
from core.auth import get_current_member
from core.models import Member
from skills.registry import (
    create_or_update_skill,
    get_skill_db,
    get_skill_version,
    list_skill_versions,
    list_skills_db,
    soft_delete_skill,
)

router = APIRouter(prefix="/skills", tags=["skills"])


class CreateSkillRequest(BaseModel):
    slug: str
    name: str
    description: str = ""
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get("")
@router.get("/")
async def list_skills(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_skills", member.organization_id)
    return await list_skills_db(member.organization_id)


@router.post("")
@router.post("/")
async def create_skill(
    req: CreateSkillRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "skill.write", f"skill:{req.slug}")

    # Guard: filesystem-sourced skills are read-only via API
    existing = await get_skill_db(member.organization_id, req.slug)
    if existing is not None and existing.get("source") == "filesystem":
        raise HTTPException(
            status_code=400,
            detail="Filesystem skills are read-only. Use the filesystem to update them.",
        )

    result = await create_or_update_skill(
        org_id=member.organization_id,
        slug=req.slug,
        name=req.name,
        description=req.description,
        content=req.content,
        metadata=req.metadata,
        created_by=str(member.id),
    )
    return {"skill_id": result["skill_id"], "version": result["version"], "slug": req.slug}


@router.get("/{slug}/versions")
async def list_versions(
    slug: str,
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "view_skill", slug)
    skill = await get_skill_db(member.organization_id, slug)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return await list_skill_versions(str(skill["id"]))


@router.get("/{slug}/versions/{version}")
async def get_version(
    slug: str,
    version: int,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "view_skill", slug)
    skill = await get_skill_db(member.organization_id, slug)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    row = await get_skill_version(str(skill["id"]), version)
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return {
        "version": row["version"],
        "content": row["content"],
        "metadata": row["metadata"],
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
        "created_by": row.get("created_by"),
    }


@router.delete("/{slug}")
async def delete_skill(
    slug: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "skill.write", f"skill:{slug}")

    existing = await get_skill_db(member.organization_id, slug)
    if existing is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    if existing.get("source") == "filesystem":
        raise HTTPException(
            status_code=400,
            detail="Filesystem skills are read-only and cannot be deleted via API.",
        )

    await soft_delete_skill(member.organization_id, slug)
    return {"deleted": True}


@router.get("/{slug}")
async def get_skill(slug: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "view_skill", slug)
    skill = await get_skill_db(member.organization_id, slug)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill
