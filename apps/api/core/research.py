"""Research store layer — pure async data-access functions over the three research tables.

No executor or HTTP logic lives here. All writes are org-scoped and audit-logged.
All reads filter by organization_id so tenant boundaries are always enforced.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, insert, or_, select, update

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member


# ---------------------------------------------------------------------------
# Private serializers
# ---------------------------------------------------------------------------

def _uuid_or_none(v: Any) -> str | None:
    """Convert a UUID (or any value) to str, or None if falsy."""
    return str(v) if v is not None else None


def _iso_or_none(v: Any) -> str | None:
    """Convert a datetime to ISO-8601 string, or None if falsy."""
    if isinstance(v, datetime):
        return v.isoformat()
    return v  # already a str or None


def _serialize_run(row: dict) -> dict:
    """Return a JSON-safe dict for a research_runs row."""
    return {
        "id": _uuid_or_none(row.get("id")),
        "organization_id": row.get("organization_id"),
        "region": row.get("region"),
        "member_id": row.get("member_id"),
        "project_id": _uuid_or_none(row.get("project_id")),
        "persona_id": _uuid_or_none(row.get("persona_id")),
        "workspace_id": _uuid_or_none(row.get("workspace_id")),
        "question": row.get("question"),
        "depth": row.get("depth"),
        "source_scopes": row.get("source_scopes"),
        "citation_policy": row.get("citation_policy"),
        "time_budget_seconds": row.get("time_budget_seconds"),
        "status": row.get("status"),
        "plan": row.get("plan"),
        "findings": row.get("findings"),
        "limitations": row.get("limitations"),
        "report_artifact_id": _uuid_or_none(row.get("report_artifact_id")),
        "error": row.get("error"),
        "token_count": row.get("token_count"),
        "cost_estimate": row.get("cost_estimate"),
        "created_at": _iso_or_none(row.get("created_at")),
        "started_at": _iso_or_none(row.get("started_at")),
        "completed_at": _iso_or_none(row.get("completed_at")),
    }


def _serialize_citation(row: dict) -> dict:
    """Return a JSON-safe dict for a research_citations row."""
    return {
        "id": _uuid_or_none(row.get("id")),
        "organization_id": row.get("organization_id"),
        "region": row.get("region"),
        "run_id": _uuid_or_none(row.get("run_id")),
        "marker": row.get("marker"),
        "source_type": row.get("source_type"),
        "source_id": row.get("source_id"),
        "source_title": row.get("source_title"),
        "url": row.get("url"),
        "snippet": row.get("snippet"),
        "confidence": row.get("confidence"),
        "distance": row.get("distance"),
        "metadata": row.get("metadata"),
        "created_at": _iso_or_none(row.get("created_at")),
    }


def _serialize_event(row: dict) -> dict:
    """Return a JSON-safe dict for a research_events row."""
    return {
        "id": _uuid_or_none(row.get("id")),
        "organization_id": row.get("organization_id"),
        "run_id": _uuid_or_none(row.get("run_id")),
        "seq": row.get("seq"),
        "event_type": row.get("event_type"),
        "payload": row.get("payload"),
        "created_at": _iso_or_none(row.get("created_at")),
    }


# ---------------------------------------------------------------------------
# research_runs
# ---------------------------------------------------------------------------

async def create_run(
    member: Member,
    *,
    question: str,
    depth: str = "standard",
    source_scopes: dict | None = None,
    project_id: str | None = None,
    persona_id: str | None = None,
    workspace_id: str | None = None,
    citation_policy: str = "required",
    time_budget_seconds: int | None = None,
) -> str:
    """Insert a new research_runs row and return its id as a string.

    Args:
        member: The member initiating the research run.
        question: The research question to investigate.
        depth: Depth level — "quick", "standard", "exhaustive", or "trusted".
        source_scopes: Optional dict of source scope config.
        project_id: Optional project to associate the run with.
        persona_id: Optional persona for this run.
        workspace_id: Optional workspace scope.
        citation_policy: Citation handling policy — default "required".
        time_budget_seconds: Optional upper time bound for the executor.

    Returns:
        The new run_id as a str.
    """
    table = await reflect_table("research_runs")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(table)
            .values(
                organization_id=member.organization_id,
                region=settings.region,
                member_id=str(member.id),
                question=question,
                depth=depth,
                source_scopes=source_scopes or {},
                project_id=project_id,
                persona_id=persona_id,
                workspace_id=workspace_id,
                citation_policy=citation_policy,
                time_budget_seconds=time_budget_seconds,
                status="pending",
            )
            .returning(table.c.id)
        )
        run_id = str(result.scalar_one())

    await audit.log(
        "research_run_created",
        str(member.id),
        "research.create",
        organization_id=member.organization_id,
        resource_type="research_runs",
        resource_id=run_id,
        payload={"depth": depth, "question": question[:120]},
    )
    return run_id


async def get_run(run_id: str, org_id: str) -> dict | None:
    """Fetch a single research run by id, scoped to the given org.

    Args:
        run_id: The UUID string of the run.
        org_id: The organization_id to enforce tenant isolation.

    Returns:
        A serialized run dict, or None if not found / belongs to another org.
    """
    table = await reflect_table("research_runs")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table).where(
                    table.c.id == run_id,
                    table.c.organization_id == org_id,
                )
            )
        ).mappings().first()
    if row is None:
        return None
    return _serialize_run(dict(row))


async def list_runs(
    org_id: str,
    *,
    project_id: str | None = None,
    member_id: str | None = None,
    visible_project_ids: list[str] | None = None,
    include_org_wide: bool = False,
    limit: int = 50,
) -> list[dict]:
    """List research runs for an org, newest first.

    Args:
        org_id: Tenant scope.
        project_id: Optional filter by project.
        limit: Maximum rows to return.

    Returns:
        List of serialized run dicts.
    """
    table = await reflect_table("research_runs")
    stmt = (
        select(table)
        .where(table.c.organization_id == org_id)
        .order_by(table.c.created_at.desc())
        .limit(limit)
    )
    if project_id is not None:
        stmt = stmt.where(table.c.project_id == project_id)
    if member_id and not include_org_wide:
        visibility = [table.c.member_id == member_id]
        if visible_project_ids:
            visibility.append(table.c.project_id.in_(visible_project_ids))
        stmt = stmt.where(or_(*visibility))

    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [_serialize_run(dict(r)) for r in rows]


async def update_run(run_id: str, org_id: str, **fields: Any) -> None:
    """Patch allowed fields on a research run.

    Silently ignores an empty fields dict (no-op). Only updates the fields
    that are explicitly passed; does not null-out missing fields.

    Args:
        run_id: The run to update.
        org_id: Tenant scope — must match the run's organization_id.
        **fields: Keyword arguments for columns to update.
    """
    if not fields:
        return
    table = await reflect_table("research_runs")
    async with engine.begin() as conn:
        await conn.execute(
            update(table)
            .where(
                table.c.id == run_id,
                table.c.organization_id == org_id,
            )
            .values(**fields)
        )


async def reset_run_progress(run_id: str, org_id: str) -> None:
    """Delete a run's prior citations and events so re-execution starts clean.

    Called by the executor before a (re-)run so that startup recovery of an
    interrupted run cannot duplicate citations or collide [S#] markers. Safe
    because research performs no external writes — a fresh attempt simply
    rebuilds the read-only evidence. The run row itself is left intact.

    Args:
        run_id: The run whose progress to clear.
        org_id: Tenant scope.
    """
    citations = await reflect_table("research_citations")
    events = await reflect_table("research_events")
    async with engine.begin() as conn:
        await conn.execute(
            delete(citations).where(
                citations.c.run_id == run_id,
                citations.c.organization_id == org_id,
            )
        )
        await conn.execute(
            delete(events).where(
                events.c.run_id == run_id,
                events.c.organization_id == org_id,
            )
        )


async def get_status(run_id: str, org_id: str) -> str | None:
    """Return the current status string for a run, or None if not found.

    Used by the executor for cooperative cancellation checks.

    Args:
        run_id: The run to query.
        org_id: Tenant scope.

    Returns:
        Status string (e.g. "pending", "running", "cancelled") or None.
    """
    table = await reflect_table("research_runs")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table.c.status).where(
                    table.c.id == run_id,
                    table.c.organization_id == org_id,
                )
            )
        ).first()
    if row is None:
        return None
    return row[0]


# ---------------------------------------------------------------------------
# research_citations
# ---------------------------------------------------------------------------

async def add_citation(
    run_id: str,
    org_id: str,
    *,
    marker: str,
    source_type: str,
    snippet: str,
    source_id: str | None = None,
    source_title: str | None = None,
    url: str | None = None,
    confidence: float | None = None,
    distance: float | None = None,
    metadata: dict | None = None,
) -> str:
    """Append a citation to a research run and return its id.

    INVARIANT: snippet must be a non-empty, non-whitespace string. This is the
    "no citation without a source snippet" rule and is enforced before any DB
    write.

    Args:
        run_id: The parent research run.
        org_id: Tenant scope.
        marker: Short label used to reference this citation in the report.
        source_type: Category of the source (web|project|connector|upload|mcp).
        snippet: The verbatim text excerpt from the source. Must not be empty.
        source_id: Optional internal id of the source.
        source_title: Optional human-readable title of the source.
        url: Optional URL of the source.
        confidence: Optional relevance confidence score (0–1).
        distance: Optional embedding distance.
        metadata: Optional arbitrary key/value pairs.

    Returns:
        The new citation_id as a str.

    Raises:
        ValueError: If snippet is None or consists only of whitespace.
    """
    if not snippet or snippet.strip() == "":
        raise ValueError("citation requires a non-empty snippet")

    table = await reflect_table("research_citations")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(table)
            .values(
                organization_id=org_id,
                region=settings.region,
                run_id=run_id,
                marker=marker,
                source_type=source_type,
                snippet=snippet,
                source_id=source_id,
                source_title=source_title,
                url=url,
                confidence=confidence,
                distance=distance,
                metadata=metadata or {},
            )
            .returning(table.c.id)
        )
        citation_id = str(result.scalar_one())

    await audit.log(
        "research_citation_added",
        "chronos",
        "research.cite",
        organization_id=org_id,
        resource_type="research_citations",
        resource_id=citation_id,
        payload={"run_id": run_id, "source_type": source_type, "marker": marker},
    )
    return citation_id


async def list_citations(run_id: str, org_id: str) -> list[dict]:
    """Return all citations for a run, ordered by creation time.

    Args:
        run_id: Filter to this run.
        org_id: Tenant scope.

    Returns:
        List of serialized citation dicts, ordered by created_at asc then marker.
    """
    table = await reflect_table("research_citations")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(table)
                .where(
                    table.c.organization_id == org_id,
                    table.c.run_id == run_id,
                )
                .order_by(table.c.created_at.asc(), table.c.marker.asc())
            )
        ).mappings().all()
    return [_serialize_citation(dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# research_events
# ---------------------------------------------------------------------------

async def append_event(
    run_id: str,
    org_id: str,
    event_type: str,
    payload: dict | None = None,
) -> int:
    """Append a structured event to the run's event log and return its seq number.

    The seq is computed as MAX(seq)+1 within the transaction, so each event
    for a given run gets a monotonically increasing integer. One executor owns
    a run, so small races are acceptable.

    Args:
        run_id: The parent research run.
        org_id: Tenant scope.
        event_type: Short label for the event (e.g. "step_start", "tool_call").
        payload: Optional structured data for the event.

    Returns:
        The assigned seq integer (1-based).
    """
    table = await reflect_table("research_events")
    async with engine.begin() as conn:
        max_result = await conn.execute(
            select(func.coalesce(func.max(table.c.seq), 0)).where(
                table.c.organization_id == org_id,
                table.c.run_id == run_id,
            )
        )
        next_seq: int = max_result.scalar_one() + 1

        await conn.execute(
            insert(table).values(
                organization_id=org_id,
                region=settings.region,
                run_id=run_id,
                seq=next_seq,
                event_type=event_type,
                payload=payload or {},
            )
        )
    return next_seq


async def list_events(
    run_id: str,
    org_id: str,
    *,
    after_seq: int = 0,
) -> list[dict]:
    """Return events for a run, optionally starting after a given seq.

    Args:
        run_id: Filter to this run.
        org_id: Tenant scope.
        after_seq: Only return events with seq > after_seq (default 0 = all).

    Returns:
        List of serialized event dicts ordered by seq asc.
    """
    table = await reflect_table("research_events")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(table)
                .where(
                    table.c.organization_id == org_id,
                    table.c.run_id == run_id,
                    table.c.seq > after_seq,
                )
                .order_by(table.c.seq.asc())
            )
        ).mappings().all()
    return [_serialize_event(dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def build_source_appendix(citations: list[dict]) -> str:
    """Build a Markdown sources appendix table from a list of citation dicts.

    Returns an empty string for an empty citations list. Pipe characters in
    cell text are escaped and snippets are truncated to ~200 characters.

    Args:
        citations: List of serialized citation dicts (as returned by list_citations).

    Returns:
        A Markdown string beginning with "## Sources", or "" if citations is empty.
    """
    if not citations:
        return ""

    def _cell(value: str | None, max_len: int | None = None) -> str:
        text = (value or "").replace("|", r"\|").replace("\n", " ").replace("\r", " ")
        if max_len and len(text) > max_len:
            text = text[:max_len]
        return text

    lines = [
        "## Sources",
        "| Marker | Type | Title | URL | Snippet |",
        "|---|---|---|---|---|",
    ]
    for c in citations:
        lines.append(
            f"| {_cell(c.get('marker'))} "
            f"| {_cell(c.get('source_type'))} "
            f"| {_cell(c.get('source_title'))} "
            f"| {_cell(c.get('url'))} "
            f"| {_cell(c.get('snippet'), 200)} |"
        )
    return "\n".join(lines)
