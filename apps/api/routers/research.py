"""Deep Research — HTTP router for research runs, citations, events, and streaming.

Endpoints are org-scoped; every mutation calls permissions.check and audit.log.
The SSE stream replays persisted events first, then subscribes to live Redis
publications from the executor (channel ``research:{run_id}``).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core import audit, permissions, research
from core.auth import get_current_member
from core.config import settings
from core.models import Member
from core.redis import redis_client
from runtime.research_executor import start_research

router = APIRouter(prefix="/research", tags=["research"])

_VALID_DEPTHS = {"quick", "standard", "exhaustive", "trusted"}


class ResearchRequest(BaseModel):
    question: str
    depth: str = "standard"
    source_scopes: dict = {}
    project_id: str | None = None
    persona_id: str | None = None
    workspace_id: str | None = None
    citation_policy: str = "required"
    time_budget_seconds: int | None = None


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
    await permissions.check(member, "create_research", settings.org_id)
    if req.depth not in _VALID_DEPTHS:
        raise HTTPException(status_code=422, detail="depth must be one of quick|standard|exhaustive|trusted")

    run_id = await research.create_run(
        member,
        question=req.question,
        depth=req.depth,
        source_scopes=req.source_scopes,
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
    return await research.list_runs(member.organization_id, project_id=project_id)


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
    row = await _require_run(member, run_id)

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

async def _require_run(member: Member, run_id: str) -> dict:
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
    return run
