from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy.sql import func

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
    # Seed the authorization model so the creator owns the new project (no-op
    # unless OpenFGA is configured).
    await permissions.grant_project_role(member.id, "owner", project_id, member.organization_id)
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

    # Mirror the membership into the authorization model (no-op unless OpenFGA
    # is configured).
    await permissions.grant_project_role(
        req.member_id, req.role or "member", project_id, member.organization_id
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

    # Remove both possible relations from the authorization model (no-op unless
    # OpenFGA is configured); we don't know which role the row carried here.
    await permissions.revoke_project_role(mid, "owner", project_id)
    await permissions.revoke_project_role(mid, "member", project_id)
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
    return [dict(row) for row in rows]


async def _require_source(member: Member, project_id: str, sid: str) -> dict:
    """Return the project_sources row or raise 404 if it isn't in this project+org."""
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
    return dict(row)


_CHUNK_PREVIEW_LIMIT = 10
_CHUNK_PREVIEW_CHARS = 600


@router.get("/{project_id}/sources/{sid}")
async def get_source_detail(
    project_id: str,
    sid: str,
    member: Member = Depends(get_current_member),
) -> dict:
    """Return one source's metadata, status/warnings, and a chunk preview.

    Org + membership gated. Used by the source viewer: original artifact ref for
    download, parse/index status, a derived warning, chunk count, and first-N chunks.
    """
    await _require_member(member, project_id)
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
                .limit(_CHUNK_PREVIEW_LIMIT)
            )
        ).mappings().all()

    parse_status = source.get("parse_status")
    warning = None
    if parse_status in ("failed", "unparseable"):
        warning = f"Document could not be fully parsed (parse_status={parse_status})."
    elif source.get("index_status") == "failed":
        warning = "Indexing failed; this source is not searchable."
    elif source.get("index_status") == "revoked":
        warning = "Connector access was revoked; this source is no longer searchable."

    return {
        "id": str(source["id"]),
        "title": source.get("title"),
        "source_type": source.get("source_type"),
        "uri": source.get("uri"),
        "artifact_id": str(source["artifact_id"]) if source.get("artifact_id") else None,
        "parse_status": parse_status,
        "index_status": source.get("index_status"),
        "warning": warning,
        "chunk_count": int(total or 0),
        "chunks": [
            {
                "chunk_index": int(row["chunk_index"]),
                "content": str(row["content"])[:_CHUNK_PREVIEW_CHARS],
                "token_count": row.get("token_count"),
            }
            for row in preview_rows
        ],
    }


@router.post("/{project_id}/sources/{sid}/reindex")
async def reindex_source(
    project_id: str,
    sid: str,
    member: Member = Depends(get_current_member),
) -> dict:
    await _require_member(member, project_id)
    await _require_source(member, project_id, sid)
    await permissions.check(member, "reindex_project_source", project_id)

    from memory.source_indexing import index_source

    summary = await index_source(sid, member.organization_id)
    await audit.log(
        "source_reindexed",
        member.id,
        "projects.reindex_source",
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
    await _require_member(member, project_id)
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
        resource_type="project_sources",
        resource_id=sid,
        payload={"project_id": project_id, "deleted_chunks": deleted_chunks},
    )
    return {"deleted": True, "source_id": sid, "deleted_chunks": deleted_chunks}


class ConnectorSourceRequest(BaseModel):
    title: str
    tool: str
    args: dict = {}
    connector_id: str | None = None


@router.post("/{project_id}/sources/connector")
async def add_connector_source(
    project_id: str,
    req: ConnectorSourceRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    """Register a connector feed source. Its fetch spec lives in ``permissions``."""
    await _require_member(member, project_id)
    await permissions.check(member, "add_project_source", project_id)

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
    await _require_member(member, project_id)
    await _require_source(member, project_id, sid)
    await permissions.check(member, "sync_project_source", project_id)

    from jobs.source_sync import sync_connector_source

    summary = await sync_connector_source(sid, member.organization_id)
    await audit.log(
        "source_synced",
        member.id,
        "projects.sync_source",
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
        artifact_rows = (await conn.execute(art_stmt)).mappings().all()

    return [dict(row) for row in artifact_rows]
