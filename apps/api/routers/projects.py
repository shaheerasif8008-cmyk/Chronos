from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, insert, or_, select, update

from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member

router = APIRouter(prefix="/projects", tags=["projects"])


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _require_member(member: Member, project_id: str) -> dict:
    """Return caller's project_members row or raise 404 (don't leak existence)."""
    projects = await reflect_table("projects")
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(project_members)
                .join(projects, projects.c.id == project_members.c.project_id)
                .where(
                    project_members.c.project_id == project_id,
                    project_members.c.member_id == member.id,
                    project_members.c.organization_id == member.organization_id,
                    projects.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return dict(row)


async def _require_owner(member: Member, project_id: str) -> dict:
    """Require the caller to be an owner; raise 403 if not."""
    membership = await _require_member(member, project_id)
    if membership.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Owner role required")
    return membership


# ─── Create ───────────────────────────────────────────────────────────────────

class CreateProjectRequest(BaseModel):
    name: str
    instructions: str | None = None
    visibility: str | None = "private"


@router.post("/")
async def create_project(
    req: CreateProjectRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "create_project", settings.org_id)
    projects = await reflect_table("projects")
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        proj_result = await conn.execute(
            insert(projects)
            .values(
                organization_id=member.organization_id,
                region=settings.region,
                name=req.name,
                instructions=req.instructions,
                visibility=req.visibility or "private",
                default_tools=[],
                memory_policy="default",
                created_by=member.id,
            )
            .returning(projects.c.id)
        )
        project_id = str(proj_result.scalar_one())
        await conn.execute(
            insert(project_members)
            .values(
                organization_id=member.organization_id,
                region=settings.region,
                project_id=project_id,
                member_id=member.id,
                role="owner",
            )
        )
    await audit.log(
        "project_created",
        member.id,
        "projects.create",
        resource_type="projects",
        resource_id=project_id,
        payload={"name": req.name},
    )
    return {"project_id": project_id, "name": req.name}


# ─── List ─────────────────────────────────────────────────────────────────────

@router.get("/")
async def list_projects(member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "list_projects", settings.org_id)
    projects = await reflect_table("projects")
    project_members = await reflect_table("project_members")
    stmt = (
        select(projects)
        .join(
            project_members,
            (project_members.c.project_id == projects.c.id)
            & (project_members.c.member_id == member.id),
        )
        .where(projects.c.organization_id == member.organization_id)
        .order_by(projects.c.created_at.desc())
    )
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(row) for row in rows]


# ─── Get ──────────────────────────────────────────────────────────────────────

@router.get("/{project_id}")
async def get_project(
    project_id: str,
    member: Member = Depends(get_current_member),
) -> dict:
    await _require_member(member, project_id)
    await permissions.check(member, "view_project", project_id)
    projects = await reflect_table("projects")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(projects).where(
                    projects.c.id == project_id,
                    projects.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return dict(row)


# ─── Patch ────────────────────────────────────────────────────────────────────

class PatchProjectRequest(BaseModel):
    name: str | None = None
    instructions: str | None = None
    visibility: str | None = None
    memory_policy: str | None = None
    default_tools: list | None = None


@router.patch("/{project_id}")
async def patch_project(
    project_id: str,
    req: PatchProjectRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    await _require_owner(member, project_id)
    await permissions.check(member, "update_project", project_id)

    patch_data = req.model_dump(exclude_unset=True)
    if not patch_data:
        return {"updated": False, "project_id": project_id}

    projects = await reflect_table("projects")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(projects).where(
                    projects.c.id == project_id,
                    projects.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Project not found")
        await conn.execute(
            update(projects)
            .where(
                projects.c.id == project_id,
                projects.c.organization_id == member.organization_id,
            )
            .values(**patch_data)
        )

    await audit.log(
        "project_updated",
        member.id,
        "projects.patch",
        resource_type="projects",
        resource_id=project_id,
        payload={"fields": list(patch_data.keys())},
    )

    # Emit additional event when instructions are explicitly included in patch body.
    if "instructions" in patch_data:
        await audit.log(
            "project_instructions_updated",
            member.id,
            "projects.instructions",
            resource_type="projects",
            resource_id=project_id,
        )

    return {"updated": True, "project_id": project_id}


# ─── Delete ───────────────────────────────────────────────────────────────────

@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    member: Member = Depends(get_current_member),
) -> dict:
    await _require_owner(member, project_id)
    await permissions.check(member, "delete_project", project_id)

    projects = await reflect_table("projects")
    async with engine.begin() as conn:
        # CASCADE on project_members.project_id → projects.id handles member rows.
        await conn.execute(
            delete(projects).where(
                projects.c.id == project_id,
                projects.c.organization_id == member.organization_id,
            )
        )

    await audit.log(
        "project_deleted",
        member.id,
        "projects.delete",
        resource_type="projects",
        resource_id=project_id,
    )
    return {"deleted": True, "project_id": project_id}


# ─── Members ──────────────────────────────────────────────────────────────────

class AddMemberRequest(BaseModel):
    member_id: str
    role: str | None = "member"


@router.post("/{project_id}/members")
async def add_project_member(
    project_id: str,
    req: AddMemberRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    await _require_owner(member, project_id)
    await permissions.check(member, "add_project_member", project_id)

    members_table = await reflect_table("members")
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        # Validate the target member exists in the caller's org
        target_member = (
            await conn.execute(
                select(members_table).where(
                    members_table.c.id == req.member_id,
                    members_table.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
        if target_member is None:
            raise HTTPException(status_code=404, detail="Member not found")

        # Idempotent: check existing first (scoped to org for defense-in-depth)
        existing = (
            await conn.execute(
                select(project_members).where(
                    project_members.c.project_id == project_id,
                    project_members.c.member_id == req.member_id,
                    project_members.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
        if existing is None:
            await conn.execute(
                insert(project_members).values(
                    organization_id=member.organization_id,
                    region=settings.region,
                    project_id=project_id,
                    member_id=req.member_id,
                    role=req.role or "member",
                )
            )

    await audit.log(
        "project_member_added",
        member.id,
        "projects.add_member",
        resource_type="project_members",
        resource_id=project_id,
        payload={"new_member_id": req.member_id, "role": req.role},
    )
    return {"project_id": project_id, "member_id": req.member_id, "role": req.role or "member"}


@router.delete("/{project_id}/members/{mid}")
async def remove_project_member(
    project_id: str,
    mid: str,
    member: Member = Depends(get_current_member),
) -> dict:
    await _require_owner(member, project_id)
    await permissions.check(member, "remove_project_member", project_id)

    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        # Lock owner rows to prevent concurrent last-owner removal (TOCTOU fix).
        owner_rows = (
            await conn.execute(
                select(project_members).where(
                    project_members.c.project_id == project_id,
                    project_members.c.role == "owner",
                ).with_for_update()
            )
        ).mappings().all()
        owner_ids = [str(r["member_id"]) for r in owner_rows]
        if mid in owner_ids and len(owner_ids) <= 1:
            raise HTTPException(
                status_code=400,
                detail="Cannot remove the last owner of a project",
            )
        await conn.execute(
            delete(project_members).where(
                project_members.c.project_id == project_id,
                project_members.c.member_id == mid,
            )
        )

    await audit.log(
        "project_member_removed",
        member.id,
        "projects.remove_member",
        resource_type="project_members",
        resource_id=project_id,
        payload={"removed_member_id": mid},
    )
    return {"project_id": project_id, "member_id": mid, "removed": True}


# ─── Sub-resource lists ────────────────────────────────────────────────────────

@router.get("/{project_id}/conversations")
async def get_project_conversations(
    project_id: str,
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await _require_member(member, project_id)
    await permissions.check(member, "view_project_conversations", project_id)

    conversations = await reflect_table("conversations")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(conversations).where(
                    conversations.c.project_id == project_id,
                    conversations.c.organization_id == member.organization_id,
                )
                .order_by(conversations.c.updated_at.desc())
            )
        ).mappings().all()
    return [dict(row) for row in rows]


@router.get("/{project_id}/tasks")
async def get_project_tasks(
    project_id: str,
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await _require_member(member, project_id)
    await permissions.check(member, "view_project_tasks", project_id)

    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(tasks).where(
                    tasks.c.project_id == project_id,
                    tasks.c.organization_id == member.organization_id,
                )
                .order_by(tasks.c.created_at.desc())
            )
        ).mappings().all()
    return [dict(row) for row in rows]


@router.get("/{project_id}/artifacts")
async def get_project_artifacts(
    project_id: str,
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await _require_member(member, project_id)
    await permissions.check(member, "view_project_artifacts", project_id)

    conversations = await reflect_table("conversations")
    tasks = await reflect_table("tasks")
    artifacts = await reflect_table("artifacts")

    async with engine.begin() as conn:
        # Collect conversation IDs linked to this project
        conv_rows = (
            await conn.execute(
                select(conversations.c.id).where(
                    conversations.c.project_id == project_id,
                    conversations.c.organization_id == member.organization_id,
                )
            )
        ).mappings().all()
        conv_ids = [str(r["id"]) for r in conv_rows]

        # Collect task IDs linked to this project
        task_rows = (
            await conn.execute(
                select(tasks.c.id).where(
                    tasks.c.project_id == project_id,
                    tasks.c.organization_id == member.organization_id,
                )
            )
        ).mappings().all()
        task_ids = [str(r["id"]) for r in task_rows]

        if not conv_ids and not task_ids:
            return []

        # Build OR filter across conversation and task links
        filters = []
        if conv_ids:
            filters.append(artifacts.c.conversation_id.in_(conv_ids))
        if task_ids:
            filters.append(artifacts.c.task_id.in_(task_ids))

        art_stmt = select(artifacts).where(
            or_(*filters),
            artifacts.c.organization_id == member.organization_id,
        ).order_by(artifacts.c.created_at.desc())
        artifact_rows = (await conn.execute(art_stmt)).mappings().all()

    return [dict(row) for row in artifact_rows]
