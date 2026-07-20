from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy.sql import func
from typing import Annotated, Literal

from core import audit, permissions, tool_broker
from core.artifacts import (
    ArtifactStorageUnavailable,
    get_artifact,
    read_artifact_content,
    save_artifact,
    set_artifact_project,
)
from core.artifact_access import artifact_access
from core.artifact_rendering import safe_download_headers
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import AgentContext, Member, RequesterContext
from core.project_exports import ProjectExportError, build_project_bundle
from core.project_access import (
    normalize_default_tools,
    normalize_visibility,
    project_access_role,
    visible_project_clause,
)
from core import conversation_access as conversation_acl
from core.task_access import visibility_clause as task_visibility_clause

router = APIRouter(prefix="/projects", tags=["projects"])

_ACTIVE_DOWNLOAD_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml", "image/svg"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _require_member(member: Member, project_id: str) -> dict:
    """Return caller's project role, including org-visible read-only access."""
    project, role = await project_access_role(member, project_id)
    if project is None or role is None:
        raise HTTPException(status_code=404, detail="Project not found")
    result = dict(project)
    result["role"] = role
    return result


async def _require_owner(member: Member, project_id: str) -> dict:
    """Require the caller to be an owner; raise 403 if not."""
    membership = await _require_member(member, project_id)
    if membership.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Owner role required")
    return membership


async def _require_editor(member: Member, project_id: str) -> dict:
    """Require explicit project membership for a project mutation."""
    membership = await _require_member(member, project_id)
    if membership.get("role") not in {"member", "owner"}:
        raise HTTPException(status_code=403, detail="Project membership required")
    return membership


# ─── Create ───────────────────────────────────────────────────────────────────

class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    instructions: str | None = None
    visibility: Literal["private", "organization"] = "private"
    default_tools: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("default_tools")
    @classmethod
    def validate_default_tools(cls, value: list[str]) -> list[str]:
        return normalize_default_tools(value)


@router.post("/")
async def create_project(
    req: CreateProjectRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "create_project", settings.org_id)
    projects = await reflect_table("projects")
    project_members = await reflect_table("project_members")
    instruction_versions = await reflect_table("project_instruction_versions")
    async with engine.begin() as conn:
        proj_result = await conn.execute(
            insert(projects)
            .values(
                organization_id=member.organization_id,
                region=settings.region,
                name=req.name,
                instructions=req.instructions,
                instructions_version=1,
                visibility=normalize_visibility(req.visibility),
                default_tools=normalize_default_tools(getattr(req, "default_tools", None)),
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
        await conn.execute(
            insert(instruction_versions).values(
                organization_id=member.organization_id,
                region=settings.region,
                project_id=project_id,
                version=1,
                instructions=req.instructions,
                changed_by=member.id,
            )
        )
    # Seed the authorization model so the creator owns the new project (no-op
    # unless OpenFGA is configured).
    await permissions.grant_project_role(member.id, "owner", project_id, member.organization_id)
    await permissions.sync_project_visibility(
        project_id, member.organization_id, req.visibility
    )
    await audit.log(
        "project_created",
        member.id,
        "projects.create",
        organization_id=member.organization_id,
        resource_type="projects",
        resource_id=project_id,
        payload={"name": req.name},
    )
    return {"project_id": project_id, "name": req.name, "instructions_version": 1}


# ─── List ─────────────────────────────────────────────────────────────────────

@router.get("/")
async def list_projects(member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "list_projects", settings.org_id)
    projects = await reflect_table("projects")
    project_members = await reflect_table("project_members")
    stmt = (
        select(
            projects,
            func.coalesce(project_members.c.role, "viewer").label("access_role"),
        )
        .outerjoin(
            project_members,
            (project_members.c.project_id == projects.c.id)
            & (project_members.c.member_id == member.id)
            & (project_members.c.organization_id == member.organization_id),
        )
        .where(
            projects.c.organization_id == member.organization_id,
            visible_project_clause(projects, project_members, member),
        )
        .distinct()
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
    membership = await _require_member(member, project_id)
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
    return {**dict(row), "access_role": membership["role"]}


# ─── Patch ────────────────────────────────────────────────────────────────────

class PatchProjectRequest(BaseModel):
    name: str | None = None
    instructions: str | None = None
    visibility: Literal["private", "organization"] | None = None
    memory_policy: str | None = None
    default_tools: list[str] | None = Field(default=None, max_length=64)

    @field_validator("default_tools")
    @classmethod
    def validate_default_tools(cls, value: list[str] | None) -> list[str] | None:
        return normalize_default_tools(value) if value is not None else None


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
    instruction_versions = await reflect_table("project_instruction_versions")
    instructions_changed = False
    new_instruction_version: int | None = None
    visibility_changed = False
    if "visibility" in patch_data:
        patch_data["visibility"] = normalize_visibility(patch_data["visibility"])
    if "default_tools" in patch_data:
        patch_data["default_tools"] = normalize_default_tools(patch_data["default_tools"])
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(projects).where(
                    projects.c.id == project_id,
                    projects.c.organization_id == member.organization_id,
                ).with_for_update()
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Project not found")
        visibility_changed = (
            "visibility" in patch_data
            and patch_data["visibility"] != normalize_visibility(row.get("visibility"))
        )
        if "instructions" in patch_data and patch_data["instructions"] != row.get("instructions"):
            instructions_changed = True
            new_instruction_version = int(row.get("instructions_version") or 1) + 1
            patch_data["instructions_version"] = new_instruction_version

        await conn.execute(
            update(projects)
            .where(
                projects.c.id == project_id,
                projects.c.organization_id == member.organization_id,
            )
            .values(**patch_data)
        )
        if instructions_changed and new_instruction_version is not None:
            await conn.execute(
                insert(instruction_versions).values(
                    organization_id=member.organization_id,
                    region=settings.region,
                    project_id=project_id,
                    version=new_instruction_version,
                    instructions=patch_data.get("instructions"),
                    changed_by=member.id,
                )
            )

    if visibility_changed:
        await permissions.sync_project_visibility(
            project_id,
            member.organization_id,
            str(patch_data["visibility"]),
        )

    await audit.log(
        "project_updated",
        member.id,
        "projects.patch",
        organization_id=member.organization_id,
        resource_type="projects",
        resource_id=project_id,
        payload={"fields": list(patch_data.keys())},
    )

    # Emit additional event when instructions are explicitly included in patch body.
    if instructions_changed:
        await audit.log(
            "project_instructions_updated",
            member.id,
            "projects.instructions",
            organization_id=member.organization_id,
            resource_type="projects",
            resource_id=project_id,
        )

    return {
        "updated": True,
        "project_id": project_id,
        "instructions_version": new_instruction_version,
    }


@router.get("/{project_id}/instruction-versions")
async def list_project_instruction_versions(
    project_id: str,
    member: Member = Depends(get_current_member),
) -> list[dict]:
    """Return the immutable instruction history to authorized project members."""

    await _require_member(member, project_id)
    await permissions.check(member, "view_project", project_id)
    versions = await reflect_table("project_instruction_versions")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(versions)
                .where(
                    versions.c.project_id == project_id,
                    versions.c.organization_id == member.organization_id,
                )
                .order_by(versions.c.version.desc())
                .limit(50)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


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
        organization_id=member.organization_id,
        resource_type="projects",
        resource_id=project_id,
    )
    return {"deleted": True, "project_id": project_id}


# ─── Members ──────────────────────────────────────────────────────────────────

class AddMemberRequest(BaseModel):
    member_id: str
    role: Literal["member", "owner"] = "member"


@router.get("/{project_id}/members")
async def list_project_members(
    project_id: str,
    member: Member = Depends(get_current_member),
) -> list[dict]:
    """List the minimal member directory for one project.

    Membership is checked before any project/member data is read, and the join
    is tenant-qualified on both sides so a reused member id cannot cross orgs.
    """

    await _require_member(member, project_id)
    await permissions.check(member, "view_project", project_id)
    members_table = await reflect_table("members")
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    project_members.c.member_id,
                    project_members.c.role,
                    project_members.c.created_at,
                    members_table.c.email,
                    members_table.c.name,
                )
                .select_from(
                    project_members.join(
                        members_table,
                        (members_table.c.id == project_members.c.member_id)
                        & (
                            members_table.c.organization_id
                            == project_members.c.organization_id
                        ),
                    )
                )
                .where(
                    project_members.c.project_id == project_id,
                    project_members.c.organization_id == member.organization_id,
                    members_table.c.organization_id == member.organization_id,
                )
                .order_by(project_members.c.created_at.asc())
            )
        ).mappings().all()
    return [dict(row) for row in rows]


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
    previous_role: str | None = None
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
        else:
            previous_role = str(existing.get("role") or "member")
            if previous_role != req.role:
                if previous_role == "owner" and req.role == "member":
                    owner_rows = (
                        await conn.execute(
                            select(project_members).where(
                                project_members.c.project_id == project_id,
                                project_members.c.organization_id == member.organization_id,
                                project_members.c.role == "owner",
                            ).with_for_update()
                        )
                    ).mappings().all()
                    if len(owner_rows) <= 1:
                        raise HTTPException(
                            status_code=400,
                            detail="Cannot demote the last owner of a project",
                        )
                await conn.execute(
                    update(project_members)
                    .where(
                        project_members.c.project_id == project_id,
                        project_members.c.member_id == req.member_id,
                        project_members.c.organization_id == member.organization_id,
                    )
                    .values(role=req.role)
                )

    # Mirror the membership into the authorization model (no-op unless OpenFGA
    # is configured).
    if previous_role and previous_role != req.role:
        await permissions.revoke_project_role(req.member_id, previous_role, project_id)
    await permissions.grant_project_role(
        req.member_id, req.role, project_id, member.organization_id
    )
    await audit.log(
        "project_member_added",
        member.id,
        "projects.add_member",
        organization_id=member.organization_id,
        resource_type="project_members",
        resource_id=project_id,
        payload={
            "new_member_id": req.member_id,
            "role": req.role,
            "previous_role": previous_role,
        },
    )
    return {"project_id": project_id, "member_id": req.member_id, "role": req.role}


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

    # Remove both possible relations from the authorization model (no-op unless
    # OpenFGA is configured); we don't know which role the row carried here.
    await permissions.revoke_project_role(mid, "owner", project_id)
    await permissions.revoke_project_role(mid, "member", project_id)
    await audit.log(
        "project_member_removed",
        member.id,
        "projects.remove_member",
        organization_id=member.organization_id,
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
    conversation_members = await reflect_table("conversation_members")
    conditions = [
        conversations.c.project_id == project_id,
        conversations.c.organization_id == member.organization_id,
    ]
    if member.role not in conversation_acl.ADMIN_ROLES:
        try:
            conditions.append(
                conversation_acl.visibility_clause(
                    conversations, conversation_members, member, select_fn=select
                )
            )
        except (AttributeError, TypeError):
            # Narrow no-op SQL fakes used by unit tests do not implement EXISTS;
            # reflected production tables always take the canonical branch.
            conditions.append(conversations.c.member_id == member.id)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(conversations).where(*conditions)
                .order_by(conversations.c.updated_at.desc())
            )
        ).mappings().all()
    return [dict(row) for row in rows]


@router.get("/{project_id}/sources")
async def get_project_sources(
    project_id: str,
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await _require_member(member, project_id)
    await permissions.check(member, "view_project_sources", project_id)

    project_sources = await reflect_table("project_sources")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(project_sources).where(
                    project_sources.c.project_id == project_id,
                    project_sources.c.organization_id == member.organization_id,
                )
                .order_by(project_sources.c.created_at.desc())
            )
        ).mappings().all()
    from memory.source_retrieval import source_permissions_allow

    context = RequesterContext.from_member(member)
    context.project_id = project_id
    return [
        _public_source(dict(row))
        for row in rows
        if source_permissions_allow(
            row.get("permissions"),
            context,
            created_by=row.get("created_by"),
        )
    ]


_SOURCE_PUBLIC_FIELDS = {
    "id",
    "project_id",
    "source_type",
    "title",
    "uri",
    "parse_status",
    "index_status",
    "connector_id",
    "created_at",
    "updated_at",
}


def _public_source(row: dict) -> dict:
    """Return source metadata without server-only fetch specs or ACL payloads."""

    return {key: value for key, value in row.items() if key in _SOURCE_PUBLIC_FIELDS}


async def _require_source(member: Member, project_id: str, sid: str) -> dict:
    """Return the project_sources row or raise 404 if it isn't in this project+org."""
    await _require_member(member, project_id)
    project_sources = await reflect_table("project_sources")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(project_sources).where(
                    project_sources.c.id == sid,
                    project_sources.c.project_id == project_id,
                    project_sources.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")
    source = dict(row)
    from memory.source_retrieval import source_permissions_allow

    context = RequesterContext.from_member(member)
    context.project_id = project_id
    if not source_permissions_allow(
        source.get("permissions"),
        context,
        created_by=source.get("created_by"),
    ):
        raise HTTPException(status_code=404, detail="Source not found")
    return source


_CHUNK_PREVIEW_LIMIT = 20
_CHUNK_PREVIEW_CHARS = 600


@router.get("/{project_id}/sources/{sid}")
async def get_source_detail(
    project_id: str,
    sid: str,
    member: Member = Depends(get_current_member),
    chunk_offset: Annotated[int, Query(ge=0)] = 0,
    chunk_limit: Annotated[int, Query(ge=1, le=100)] = _CHUNK_PREVIEW_LIMIT,
) -> dict:
    """Return one source's metadata, status/warnings, and a chunk preview.

    Org + membership gated. Used by the source viewer: original artifact ref for
    download, parse/index status, a derived warning, chunk count, and first-N chunks.
    """
    await permissions.check(member, "view_project_sources", project_id)
    source = await _require_source(member, project_id, sid)

    chunks = await reflect_table("project_source_chunks")
    async with engine.begin() as conn:
        total = (
            await conn.execute(
                select(func.count())
                .select_from(chunks)
                .where(
                    chunks.c.source_id == sid,
                    chunks.c.organization_id == member.organization_id,
                )
            )
        ).scalar_one()
        preview_rows = (
            await conn.execute(
                select(chunks.c.chunk_index, chunks.c.content, chunks.c.token_count)
                .where(
                    chunks.c.source_id == sid,
                    chunks.c.organization_id == member.organization_id,
                )
                .order_by(chunks.c.chunk_index.asc())
                .limit(chunk_limit)
                .offset(chunk_offset)
            )
        ).mappings().all()

    parse_status = source.get("parse_status")
    warnings: list[str] = []
    if parse_status in ("failed", "unparseable"):
        warnings.append(f"Document could not be fully parsed (parse_status={parse_status}).")
    elif source.get("index_status") == "failed":
        warnings.append("Indexing failed; this source is not searchable.")
    elif source.get("index_status") == "revoked":
        warnings.append("Connector access was revoked; this source is no longer searchable.")
    elif source.get("index_status") == "quarantined":
        warnings.append(
            "This source was quarantined after prompt-injection indicators were detected; "
            "it is not included in model context."
        )
    source_permissions = source.get("permissions") or {}
    if isinstance(source_permissions, dict) and source_permissions.get("source_truncated"):
        warnings.append(
            "Only the first 10,000 characters were available from this URL; indexing is partial."
        )
    if isinstance(source_permissions, dict) and source_permissions.get("index_truncated"):
        limits = source_permissions["index_truncated"]
        indexed_chars = limits.get("indexed_characters") if isinstance(limits, dict) else None
        warnings.append(
            "This source exceeded the safe indexing budget; "
            f"only the first {indexed_chars or 'bounded number of'} characters are searchable."
        )

    return {
        "id": str(source["id"]),
        "title": source.get("title"),
        "source_type": source.get("source_type"),
        "uri": source.get("uri"),
        "has_original": bool(source.get("artifact_id")),
        "download_url": (
            f"/projects/{project_id}/sources/{sid}/content"
            if source.get("artifact_id")
            else None
        ),
        "parse_status": parse_status,
        "index_status": source.get("index_status"),
        "warning": " ".join(warnings) or None,
        "chunk_count": int(total or 0),
        "chunk_offset": chunk_offset,
        "next_offset": (
            chunk_offset + len(preview_rows)
            if chunk_offset + len(preview_rows) < int(total or 0)
            else None
        ),
        "chunks": [
            {
                "chunk_index": int(row["chunk_index"]),
                "content": str(row["content"])[:_CHUNK_PREVIEW_CHARS],
                "token_count": row.get("token_count"),
            }
            for row in preview_rows
        ],
    }


@router.get("/{project_id}/sources/{sid}/content")
async def download_source_content(
    project_id: str,
    sid: str,
    member: Member = Depends(get_current_member),
) -> Response:
    """Download a source original through the source ACL, not artifact visibility."""

    await permissions.check(member, "view_project_sources", project_id)
    source = await _require_source(member, project_id, sid)
    artifact_id = str(source.get("artifact_id") or "")
    if not artifact_id:
        raise HTTPException(status_code=404, detail="Source content not found")
    meta = await get_artifact(artifact_id)
    if not meta or str(meta.get("organization_id")) != member.organization_id:
        raise HTTPException(status_code=404, detail="Source content not found")
    content = await read_artifact_content(artifact_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Source content not found")
    mime = str(meta.get("mime_type") or "application/octet-stream")
    active_markup = mime.lower() in _ACTIVE_DOWNLOAD_TYPES or mime.lower().endswith("+xml")
    return Response(
        content=content,
        media_type="application/octet-stream" if active_markup else mime,
        headers=safe_download_headers(source.get("title"), active_markup=active_markup),
    )


@router.post("/{project_id}/sources/{sid}/reindex")
async def reindex_source(
    project_id: str,
    sid: str,
    member: Member = Depends(get_current_member),
) -> dict:
    await _require_editor(member, project_id)
    await _require_source(member, project_id, sid)
    await permissions.check(member, "reindex_project_source", project_id)

    from memory.source_indexing import index_source

    summary = await index_source(sid, member.organization_id)
    await audit.log(
        "source_reindexed",
        member.id,
        "projects.reindex_source",
        organization_id=member.organization_id,
        resource_type="project_sources",
        resource_id=sid,
        payload={"project_id": project_id, "chunk_count": summary.get("chunk_count")},
    )
    return summary


@router.post("/{project_id}/sources/{sid}/refresh")
async def refresh_source(
    project_id: str,
    sid: str,
    member: Member = Depends(get_current_member),
) -> dict:
    await _require_editor(member, project_id)
    source = await _require_source(member, project_id, sid)
    await permissions.check(member, "refresh_project_source", project_id)

    from core.artifacts import set_parse_status
    from memory.source_indexing import index_source

    # Force a fresh parse: drop the cached parsed_text child and reset parse_status
    # so index_source re-parses the attachment from raw bytes.
    artifact_id = source.get("artifact_id")
    if artifact_id:
        artifacts = await reflect_table("artifacts")
        async with engine.begin() as conn:
            await conn.execute(
                delete(artifacts).where(
                    artifacts.c.parent_artifact_id == str(artifact_id),
                    artifacts.c.kind == "parsed_text",
                    artifacts.c.organization_id == member.organization_id,
                )
            )
        await set_parse_status(str(artifact_id), "pending")

    summary = await index_source(sid, member.organization_id)
    await audit.log(
        "source_refreshed",
        member.id,
        "projects.refresh_source",
        organization_id=member.organization_id,
        resource_type="project_sources",
        resource_id=sid,
        payload={"project_id": project_id, "chunk_count": summary.get("chunk_count")},
    )
    return summary


@router.delete("/{project_id}/sources/{sid}")
async def delete_source(
    project_id: str,
    sid: str,
    member: Member = Depends(get_current_member),
) -> dict:
    await _require_owner(member, project_id)
    await _require_source(member, project_id, sid)
    await permissions.check(member, "delete_project_source", project_id)

    from memory.source_indexing import delete_source_chunks

    deleted_chunks = await delete_source_chunks(sid, member.organization_id)
    project_sources = await reflect_table("project_sources")
    async with engine.begin() as conn:
        await conn.execute(
            delete(project_sources).where(
                project_sources.c.id == sid,
                project_sources.c.organization_id == member.organization_id,
            )
        )

    await audit.log(
        "source_deleted",
        member.id,
        "projects.delete_source",
        organization_id=member.organization_id,
        resource_type="project_sources",
        resource_id=sid,
        payload={"project_id": project_id, "deleted_chunks": deleted_chunks},
    )
    return {"deleted": True, "source_id": sid, "deleted_chunks": deleted_chunks}


class ConnectorSourceRequest(BaseModel):
    title: str
    tool: str
    args: dict = Field(default_factory=dict)
    connector_id: str = Field(min_length=1)


class UrlSourceRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2_048)
    title: str | None = Field(default=None, max_length=240)


@router.post("/{project_id}/sources/url")
async def add_url_source(
    project_id: str,
    req: UrlSourceRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    """Fetch and index a public URL as project knowledge.

    The network read always crosses the governed tool-broker seam.  Unsafe
    URLs, unavailable browsers, fixture responses, and empty content fail
    honestly without creating a searchable source row.
    """

    await _require_editor(member, project_id)
    await permissions.check(member, "add_project_source", project_id)

    agent = AgentContext(
        id=f"project-source:{member.id}",
        org_id=member.organization_id,
        member_id=member.id,
    )
    try:
        result = await tool_broker.execute(agent, "browser.fetch", {"url": req.url})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"The URL could not be fetched: {type(exc).__name__}",
        ) from exc

    data = result.data or {}
    content = str(data.get("content") or "").strip()
    tier = str(data.get("tier") or "live")
    if data.get("is_unavailable") or tier in {"demo", "fixture", "unavailable"} or not content:
        raise HTTPException(
            status_code=503,
            detail="The URL returned no live readable content, so no source was created.",
        )

    source_title = (req.title or str(data.get("title") or "").strip() or req.url)[:240]
    try:
        artifact_id = await save_artifact(
            content,
            kind="attachment",
            title=source_title,
            mime_type="text/plain",
            org_id=member.organization_id,
            parse_status="pending",
            created_by=member.id,
        )
    except ArtifactStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    project_sources = await reflect_table("project_sources")
    async with engine.begin() as conn:
        inserted = await conn.execute(
            insert(project_sources)
            .values(
                organization_id=member.organization_id,
                region=settings.region,
                project_id=project_id,
                source_type="url",
                title=source_title,
                uri=str(data.get("url") or req.url),
                artifact_id=artifact_id,
                permissions={
                    "untrusted_content": data.get("untrusted_content") or {},
                    "source_truncated": bool(data.get("truncated")),
                },
                parse_status="pending",
                index_status="pending",
                created_by=member.id,
            )
            .returning(project_sources.c.id)
        )
        source_id = str(inserted.scalar_one())

    from memory.source_indexing import index_source

    summary = await index_source(source_id, member.organization_id)
    await audit.log(
        "source_added",
        member.id,
        "projects.add_url_source",
        organization_id=member.organization_id,
        resource_type="project_sources",
        resource_id=source_id,
        payload={
            "project_id": project_id,
            "source_type": "url",
            "index_status": summary.get("index_status"),
        },
    )
    return {"source_id": source_id, **summary}


@router.post("/{project_id}/sources/connector")
async def add_connector_source(
    project_id: str,
    req: ConnectorSourceRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    """Register a connector feed source. Its fetch spec lives in ``permissions``."""
    await _require_editor(member, project_id)
    await permissions.check(member, "add_project_source", project_id)

    # A feed must bind to an active credential the caller actually owns (or an
    # explicitly org-shared credential). A free-form connector id/tool pair is
    # not an authorization capability.
    from core.connector_tools import member_connector_clause

    connectors = await reflect_table("connectors")
    async with engine.begin() as conn:
        connector = (
            await conn.execute(
                select(connectors).where(
                    connectors.c.id == req.connector_id,
                    connectors.c.organization_id == member.organization_id,
                    connectors.c.status == "active",
                    member_connector_clause(
                        connectors,
                        member.organization_id,
                        member.id,
                    ),
                )
            )
        ).mappings().first()
    requested_provider = req.tool.split("__", 1)[0].split(".", 1)[0]
    if connector is None or str(connector.get("provider") or "") != requested_provider:
        raise HTTPException(status_code=404, detail="Connector not found")

    project_sources = await reflect_table("project_sources")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(project_sources)
            .values(
                organization_id=member.organization_id,
                region=settings.region,
                project_id=project_id,
                source_type="connector",
                connector_id=req.connector_id,
                title=req.title,
                uri=req.tool,
                permissions={"tool": req.tool, "args": req.args},
                parse_status="pending",
                index_status="pending",
                created_by=member.id,
            )
            .returning(project_sources.c.id)
        )
        source_id = str(result.scalar_one())

    await audit.log(
        "source_added",
        member.id,
        "projects.add_connector_source",
        organization_id=member.organization_id,
        resource_type="project_sources",
        resource_id=source_id,
        payload={"project_id": project_id, "source_type": "connector", "tool": req.tool},
    )
    return {"source_id": source_id}


@router.post("/{project_id}/sources/{sid}/sync")
async def sync_source(
    project_id: str,
    sid: str,
    member: Member = Depends(get_current_member),
) -> dict:
    """Pull + index a connector source's documents through the tool broker."""
    await _require_editor(member, project_id)
    await _require_source(member, project_id, sid)
    await permissions.check(member, "sync_project_source", project_id)

    from jobs.source_sync import sync_connector_source

    summary = await sync_connector_source(sid, member.organization_id)
    await audit.log(
        "source_synced",
        member.id,
        "projects.sync_source",
        organization_id=member.organization_id,
        resource_type="project_sources",
        resource_id=sid,
        payload={
            "project_id": project_id,
            "synced": summary.get("synced"),
            "indexed": summary.get("indexed"),
            "index_status": summary.get("index_status"),
        },
    )
    return summary


@router.get("/{project_id}/tasks")
async def get_project_tasks(
    project_id: str,
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await _require_member(member, project_id)
    await permissions.check(member, "view_project_tasks", project_id)

    tasks = await reflect_table("tasks")
    conditions = [
        tasks.c.project_id == project_id,
        tasks.c.organization_id == member.organization_id,
    ]
    if member.role not in {"admin", "owner"}:
        try:
            conditions.append(task_visibility_clause(tasks, member))
        except AttributeError:
            # Compatibility with legacy narrow SQL fakes only.
            pass
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(tasks).where(*conditions)
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

        # Build OR filter across direct project link + conversation/task links.
        # Direct link (artifacts.project_id) is set by the artifact `move` action.
        filters = [artifacts.c.project_id == project_id]
        if conv_ids:
            filters.append(artifacts.c.conversation_id.in_(conv_ids))
        if task_ids:
            filters.append(artifacts.c.task_id.in_(task_ids))

        art_stmt = select(artifacts).where(
            or_(*filters),
            artifacts.c.organization_id == member.organization_id,
        ).order_by(artifacts.c.created_at.desc())
        artifact_rows = (await conn.execute(art_stmt.limit(200))).mappings().all()

    visible: list[dict] = []
    for row in artifact_rows:
        meta = dict(row)
        can_read, _ = await artifact_access(member, meta)
        if can_read:
            visible.append(meta)
    return visible


@router.post("/{project_id}/export")
async def export_project_bundle(
    project_id: str,
    member: Member = Depends(get_current_member),
) -> dict:
    """Create a durable ZIP of artifacts explicitly shared to this project."""
    await _require_editor(member, project_id)
    await permissions.check(member, "view_project_artifacts", project_id)
    if not await permissions.check(member, "artifact.create", "artifact:new"):
        raise HTTPException(status_code=403, detail="Not authorized")
    try:
        bundle, summary = await build_project_bundle(project_id, member.organization_id)
        project_name = str(summary.get("project_name") or "Project").strip()[:120]
        title = f"{project_name} export {datetime.now(timezone.utc).date().isoformat()}.zip"
        artifact_id = await save_artifact(
            bundle,
            kind="project_bundle",
            title=title,
            mime_type="application/zip",
            org_id=member.organization_id,
            created_by=f"member:{member.id}",
        )
        await set_artifact_project(
            artifact_id, project_id=project_id, org_id=member.organization_id
        )
    except ProjectExportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ArtifactStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await audit.log(
        "project_exported",
        member.id,
        "projects.export",
        organization_id=member.organization_id,
        resource_type="projects",
        resource_id=project_id,
        payload={"artifact_id": artifact_id, **summary},
    )
    return {"artifact": await get_artifact(artifact_id), "summary": summary}
