"""Deep Research — HTTP router for research runs, citations, events, and streaming.

Endpoints are org-scoped; every mutation calls permissions.check and audit.log.
The SSE stream replays persisted events first, then subscribes to live Redis
publications from the executor (channel ``research:{run_id}``).
"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from core import audit, permissions, research, tool_broker
from core.artifacts import ArtifactStorageUnavailable
from core.auth import get_current_member
from core.db import engine, reflect_table
from core.models import AgentContext, Member
from core.project_access import visible_project_clause
from core.research_exports import ResearchExportError, create_research_export
from core.redis import redis_client
from runtime.research_executor import start_research

router = APIRouter(prefix="/research", tags=["research"])

_VALID_DEPTHS = {"quick", "standard", "exhaustive", "trusted"}
_VALID_CITATION_POLICIES = {"required", "best_effort"}
_ORG_ADMIN_ROLES = {"admin", "owner"}
_DOMAIN_PATTERN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


async def _visible_project_ids(member: Member) -> list[str]:
    projects = await reflect_table("projects")
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                projects.select()
                .with_only_columns(projects.c.id)
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
            )
        ).all()
    return [str(row[0]) for row in rows]


async def _editable_project_ids(member: Member) -> list[str]:
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                project_members.select().with_only_columns(project_members.c.project_id).where(
                    project_members.c.organization_id == member.organization_id,
                    project_members.c.member_id == member.id,
                )
            )
        ).all()
    return [str(row[0]) for row in rows]


class ResearchSourceScopes(BaseModel):
    web: bool = True
    project: bool = False
    connector: bool = False
    upload: bool = False
    mcp: bool = False
    mcp_tools: list["MCPResearchTool"] = Field(default_factory=list, max_length=5)
    allowed_domains: list[str] = Field(default_factory=list, max_length=50)
    disallowed_domains: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("allowed_domains", "disallowed_domains")
    @classmethod
    def normalize_domains(cls, domains: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in domains:
            domain = str(raw).strip().lower().rstrip(".")
            if not _DOMAIN_PATTERN.fullmatch(domain):
                raise ValueError(f"invalid research domain: {raw}")
            normalized.append(domain)
        return sorted(set(normalized))

    @model_validator(mode="after")
    def validate_sources(self) -> "ResearchSourceScopes":
        if not (self.web or self.project or self.connector or self.upload or self.mcp):
            raise ValueError("at least one research source is required")
        if self.mcp != bool(self.mcp_tools):
            raise ValueError("MCP research requires one or more selected read-only tools")
        overlap = set(self.allowed_domains) & set(self.disallowed_domains)
        if overlap:
            raise ValueError(f"domains cannot be both allowed and blocked: {', '.join(sorted(overlap))}")
        return self


class MCPResearchTool(BaseModel):
    server_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    arguments: dict = Field(default_factory=dict)
    query_argument: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    title: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def validate_argument_budget(self) -> "MCPResearchTool":
        if len(json.dumps(self.arguments, default=str)) > 10_000:
            raise ValueError("MCP research arguments exceed the 10,000 character limit")
        if any(str(key).startswith("__") for key in self.arguments):
            raise ValueError("MCP research arguments cannot use reserved Chronos keys")
        if self.query_argument and self.query_argument.startswith("__"):
            raise ValueError("MCP query argument cannot use a reserved Chronos key")
        return self


class ResearchRequest(BaseModel):
    question: str = Field(min_length=3, max_length=10_000)
    depth: str = "standard"
    source_scopes: ResearchSourceScopes = Field(default_factory=ResearchSourceScopes)
    project_id: str | None = None
    persona_id: str | None = None
    workspace_id: str | None = None
    citation_policy: str = "required"
    time_budget_seconds: int | None = Field(default=None, ge=15, le=3600)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, question: str) -> str:
        return question.strip()

    @model_validator(mode="after")
    def validate_run_policy(self) -> "ResearchRequest":
        if self.citation_policy not in _VALID_CITATION_POLICIES:
            raise ValueError("citation_policy must be required or best_effort")
        if self.depth == "trusted" and self.source_scopes.web and not self.source_scopes.allowed_domains:
            raise ValueError("trusted research requires at least one allowed web domain")
        if (self.source_scopes.project or self.source_scopes.connector or self.source_scopes.upload) and not self.project_id:
            raise ValueError("project, connector, or uploaded-file research requires a project_id")
        return self


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/")
async def create_research_run(
    req: ResearchRequest, member: Member = Depends(get_current_member)
) -> dict:
    """Create and immediately start a new research run.

    Args:
        req: Research parameters including question and depth.
        member: Authenticated member (injected by FastAPI).

    Returns:
        Dict with run_id and initial status "pending".
    """
    await permissions.check(member, "create_research", member.organization_id)
    if req.depth not in _VALID_DEPTHS:
        raise HTTPException(status_code=422, detail="depth must be one of quick|standard|exhaustive|trusted")

    if req.project_id is not None and req.project_id not in await _editable_project_ids(member):
        raise HTTPException(status_code=404, detail="Project not found")
    run_id = await research.create_run(
        member,
        question=req.question,
        depth=req.depth,
        source_scopes=req.source_scopes.model_dump(),
        project_id=req.project_id,
        persona_id=req.persona_id,
        workspace_id=req.workspace_id,
        citation_policy=req.citation_policy,
        time_budget_seconds=req.time_budget_seconds,
    )
    await start_research(run_id, member.organization_id)
    return {"run_id": run_id, "status": "pending"}


@router.get("/")
async def list_research_runs(
    project_id: str | None = None,
    member: Member = Depends(get_current_member),
) -> list[dict]:
    """List research runs for the authenticated member's org.

    Args:
        project_id: Optional filter to a specific project.
        member: Authenticated member (injected by FastAPI).

    Returns:
        List of serialized run dicts, newest first.
    """
    visible_projects = [] if member.role in _ORG_ADMIN_ROLES else await _visible_project_ids(member)
    if project_id is not None and member.role not in _ORG_ADMIN_ROLES and project_id not in visible_projects:
        raise HTTPException(status_code=404, detail="Project not found")
    return await research.list_runs(
        member.organization_id,
        project_id=project_id,
        member_id=member.id,
        visible_project_ids=visible_projects,
        include_org_wide=member.role in _ORG_ADMIN_ROLES,
    )


@router.get("/mcp-tools")
async def list_research_mcp_tools(
    server_id: str,
    member: Member = Depends(get_current_member),
) -> dict:
    """Discover only MCP tools whose server declares them read-only.

    Discovery crosses the governed platform broker seam. Tools with absent or
    false ``readOnlyHint`` annotations are intentionally unavailable to
    unattended research runs.
    """
    await permissions.check(member, "list_mcp_servers", member.organization_id)
    agent = AgentContext(
        id=f"research-discovery:{member.id}",
        org_id=member.organization_id,
        member_id=member.id,
    )
    result = await tool_broker.execute(
        agent,
        "platform.actions",
        {"platform_id": f"mcp:{server_id}"},
    )
    actions = [
        action
        for action in result.data.get("actions") or []
        if isinstance(action, dict)
        and isinstance(action.get("annotations"), dict)
        and action["annotations"].get("readOnlyHint") is True
    ]
    return {
        "server_id": server_id,
        "actions": actions,
        "excluded_count": max(0, len(result.data.get("actions") or []) - len(actions)),
    }


@router.get("/{run_id}")
async def get_research_run(
    run_id: str, member: Member = Depends(get_current_member)
) -> dict:
    """Fetch a single research run by id.

    Args:
        run_id: UUID of the run.
        member: Authenticated member (injected by FastAPI).

    Returns:
        Serialized run dict.
    """
    return await _require_run(member, run_id)


@router.get("/{run_id}/citations")
async def list_run_citations(
    run_id: str, member: Member = Depends(get_current_member)
) -> list[dict]:
    """Return citations for a research run.

    Args:
        run_id: UUID of the run.
        member: Authenticated member (injected by FastAPI).

    Returns:
        List of serialized citation dicts.
    """
    await _require_run(member, run_id)
    return await research.list_citations(run_id, member.organization_id)


class ResearchExportRequest(BaseModel):
    format: str = Field(pattern=r"^(docx|pdf)$")


@router.post("/{run_id}/export")
async def export_research_report(
    run_id: str,
    req: ResearchExportRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    """Create a durable DOCX or PDF child artifact for a completed report."""
    run = await _require_run(member, run_id)
    if not await permissions.check(member, "artifact.create", "artifact:new"):
        raise HTTPException(status_code=403, detail="Not authorized")
    citations = await research.list_citations(run_id, member.organization_id)
    try:
        artifact, reused = await create_research_export(
            run,
            citations,
            req.format,  # type: ignore[arg-type]
            org_id=member.organization_id,
            created_by=f"member:{member.id}",
        )
    except ResearchExportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ArtifactStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await audit.log(
        "research_report_exported",
        member.id,
        "research.export",
        organization_id=member.organization_id,
        resource_type="research_runs",
        resource_id=run_id,
        payload={
            "artifact_id": str(artifact["id"]),
            "format": req.format,
            "reused": reused,
            "citation_count": len(citations),
            "limitations_preserved": bool(run.get("limitations")),
        },
    )
    return {"artifact": artifact, "reused": reused}


@router.get("/{run_id}/events")
async def list_run_events(
    run_id: str,
    after_seq: int = 0,
    member: Member = Depends(get_current_member),
) -> list[dict]:
    """Return structured events for a research run.

    Args:
        run_id: UUID of the run.
        after_seq: Only return events with seq > after_seq.
        member: Authenticated member (injected by FastAPI).

    Returns:
        List of serialized event dicts.
    """
    await _require_run(member, run_id)
    return await research.list_events(run_id, member.organization_id, after_seq=after_seq)


@router.get("/{run_id}/stream")
async def stream_research_run(
    run_id: str, member: Member = Depends(get_current_member)
) -> StreamingResponse:
    """SSE stream that replays persisted events then follows live Redis publications.

    Replay phase: yields each persisted event (from research_events) as a data frame.
    Live phase: subscribes to ``research:{run_id}`` and forwards raw JSON strings that
    the executor publishes.

    Args:
        run_id: UUID of the run.
        member: Authenticated member (injected by FastAPI).

    Returns:
        StreamingResponse with media_type text/event-stream.
    """
    await permissions.check(member, "stream_research", run_id)
    await _require_run(member, run_id)

    org = member.organization_id

    async def events():
        # --- Replay persisted events ---
        persisted = await research.list_events(run_id, org)
        for ev in persisted:
            yield f"data: {json.dumps(ev, default=str)}\n\n"

        # --- Subscribe to live Redis channel ---
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(f"research:{run_id}")
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30)
                if not message:
                    continue
                yield f"data: {message['data']}\n\n"
        finally:
            await pubsub.unsubscribe(f"research:{run_id}")
            await pubsub.close()

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/{run_id}/cancel")
async def cancel_research_run(
    run_id: str, member: Member = Depends(get_current_member)
) -> dict:
    """Cancel an in-progress research run.

    If the run is already in a terminal state (complete, failed, cancelled),
    returns ``cancelled: False`` as a no-op. Otherwise sets status to
    "cancelled" and returns ``cancelled: True``.

    Args:
        run_id: UUID of the run.
        member: Authenticated member (injected by FastAPI).

    Returns:
        Dict with run_id, status, and cancelled bool.
    """
    await permissions.check(member, "cancel_research", run_id)
    row = await _require_run(member, run_id, write=True)

    if row["status"] in {"complete", "failed", "cancelled"}:
        return {"run_id": run_id, "status": row["status"], "cancelled": False}

    await research.update_run(run_id, member.organization_id, status="cancelled")
    await audit.log(
        "research_run_cancelled",
        member.id,
        "research.cancel",
        organization_id=member.organization_id,
        resource_type="research_runs",
        resource_id=run_id,
    )
    return {"run_id": run_id, "status": "cancelled", "cancelled": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _require_run(member: Member, run_id: str, *, write: bool = False) -> dict:
    """Fetch a research run and 404 if not found or belongs to another org.

    Args:
        member: Authenticated member — used to enforce org scope.
        run_id: UUID of the run.

    Returns:
        Serialized run dict.

    Raises:
        HTTPException: 404 if not found.
    """
    run = await research.get_run(run_id, member.organization_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Research run not found")
    if member.role in _ORG_ADMIN_ROLES:
        return run
    if str(run.get("member_id")) == str(member.id):
        return run
    if not write and run.get("project_id") and str(run["project_id"]) in await _visible_project_ids(member):
        return run
    raise HTTPException(status_code=404, detail="Research run not found")
