"""DB-backed tests for the deep-research store layer (core/research.py) and executor
(runtime/research_executor.py).

Tests require the Postgres instance at localhost:55432 (the isolated test DB).
All tests are automatically skipped if that instance is unreachable.
"""
from __future__ import annotations

import os
import socket
import uuid

import pytest


# ---------------------------------------------------------------------------
# DB-reachability guard — mirrors pattern in test_memory_parity.py
# ---------------------------------------------------------------------------

def _db_reachable() -> bool:
    host, _, port_str = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://chronos:chronos@localhost:55432/chronos"
    ).rpartition("@")[-1].partition("/")[0].rpartition(":")
    port = int(port_str) if port_str.isdigit() else 55432
    try:
        with socket.create_connection((host or "localhost", port), timeout=1):
            return True
    except OSError:
        return False


_requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _member(org_id: str = "default"):
    from core.models import Member

    return Member(
        id=str(uuid.uuid4()),
        organization_id=org_id,
        email="test@example.com",
        role="user",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@_requires_db
@pytest.mark.asyncio
async def test_add_citation_requires_snippet():
    """add_citation raises ValueError for empty/whitespace snippet; valid snippet persists."""
    from core.research import add_citation, create_run, list_citations

    member = _member()
    run_id = await create_run(member, question="What is the capital of France?")

    # Empty string must raise
    with pytest.raises(ValueError, match="non-empty snippet"):
        await add_citation(
            run_id, member.organization_id, marker="[1]", source_type="web", snippet=""
        )

    # Whitespace-only must raise
    with pytest.raises(ValueError, match="non-empty snippet"):
        await add_citation(
            run_id, member.organization_id, marker="[1]", source_type="web", snippet="   "
        )

    # Valid snippet persists and is visible in list_citations
    cid = await add_citation(
        run_id,
        member.organization_id,
        marker="[1]",
        source_type="web",
        snippet="Paris is the capital of France.",
        source_title="Wikipedia",
        url="https://en.wikipedia.org/wiki/Paris",
    )
    assert cid is not None
    citations = await list_citations(run_id, member.organization_id)
    assert len(citations) == 1
    assert citations[0]["id"] == cid
    assert citations[0]["snippet"] == "Paris is the capital of France."
    assert citations[0]["marker"] == "[1]"


@_requires_db
@pytest.mark.asyncio
async def test_citations_and_runs_are_tenant_scoped():
    """Runs and citations are invisible to a different org_id."""
    from core.research import add_citation, create_run, get_run, list_citations, list_runs

    member = _member(org_id="default")
    other_org = f"other-org-{uuid.uuid4().hex[:8]}"

    run_id = await create_run(member, question="Tenant isolation test?")
    await add_citation(
        run_id,
        member.organization_id,
        marker="[A]",
        source_type="doc",
        snippet="This snippet belongs to the default org.",
    )

    # Cross-org get_run returns None
    assert await get_run(run_id, other_org) is None

    # Cross-org get_run on correct org returns the run
    run = await get_run(run_id, member.organization_id)
    assert run is not None
    assert run["id"] == run_id

    # Cross-org list_citations returns []
    assert await list_citations(run_id, other_org) == []

    # list_runs for other_org does not include our run
    other_runs = await list_runs(other_org)
    assert all(r["id"] != run_id for r in other_runs)


@_requires_db
@pytest.mark.asyncio
async def test_append_event_monotonic_seq():
    """Events get sequential seqs starting at 1; after_seq filter works."""
    from core.research import append_event, create_run, list_events

    member = _member()
    run_id = await create_run(member, question="Seq test?")

    seq1 = await append_event(run_id, member.organization_id, "step_start", {"step": 1})
    seq2 = await append_event(run_id, member.organization_id, "tool_call", {"tool": "search"})
    seq3 = await append_event(run_id, member.organization_id, "step_done", {"result": "ok"})

    assert seq1 == 1
    assert seq2 == 2
    assert seq3 == 3

    all_events = await list_events(run_id, member.organization_id)
    assert len(all_events) == 3
    assert [e["seq"] for e in all_events] == [1, 2, 3]

    # after_seq=1 should return only seqs 2 and 3
    tail = await list_events(run_id, member.organization_id, after_seq=1)
    assert len(tail) == 2
    assert [e["seq"] for e in tail] == [2, 3]


@_requires_db
@pytest.mark.asyncio
async def test_update_run_and_get_status():
    """update_run mutates fields; get_status reflects them; no-op update is safe."""
    from core.research import create_run, get_run, get_status, update_run

    member = _member()
    run_id = await create_run(member, question="Update test?")

    # Freshly created run is pending
    assert await get_status(run_id, member.organization_id) == "pending"

    # Update status and limitations
    await update_run(run_id, member.organization_id, status="running", limitations="time limited")

    run = await get_run(run_id, member.organization_id)
    assert run is not None
    assert run["status"] == "running"
    assert run["limitations"] == "time limited"

    assert await get_status(run_id, member.organization_id) == "running"

    # No-op update (empty fields) must not raise
    await update_run(run_id, member.organization_id)

    # Status unchanged after no-op
    assert await get_status(run_id, member.organization_id) == "running"

    # get_status for unknown run_id returns None
    assert await get_status(str(uuid.uuid4()), member.organization_id) is None


@_requires_db
@pytest.mark.asyncio
async def test_build_source_appendix():
    """build_source_appendix returns correct markdown; empty list returns ''."""
    from core.research import build_source_appendix

    # Empty list
    assert build_source_appendix([]) == ""

    citations = [
        {
            "marker": "[1]",
            "source_type": "web",
            "source_title": "Wikipedia | Paris",
            "url": "https://en.wikipedia.org/wiki/Paris",
            "snippet": "Paris is the capital of France.",
        },
        {
            "marker": "[2]",
            "source_type": "doc",
            "source_title": None,
            "url": None,
            "snippet": "France is a country in Western Europe.",
        },
    ]

    result = build_source_appendix(citations)

    # Must start with the heading
    assert result.startswith("## Sources")

    # Must have the header row
    assert "| Marker | Type | Title | URL | Snippet |" in result

    # Both markers present
    assert "[1]" in result
    assert "[2]" in result

    # Pipe characters in source_title must be escaped
    assert r"Wikipedia \| Paris" in result

    # Separator row present
    assert "|---|---|---|---|---|" in result


# ===========================================================================
# Executor tests (runtime/research_executor.py)
# ===========================================================================

import pytest
from unittest.mock import AsyncMock, patch


def _fake_search_result(*, tier: str = "live", is_fallback: bool = False):
    """Return a ToolResult-like object simulating a live browser.search response."""
    from core.models import ToolResult

    return ToolResult(
        summary="Search results",
        data={
            "results": [
                {
                    "title": "Test Title",
                    "snippet": "web snippet for testing",
                    "url": "https://example.com/a",
                }
            ],
            "tier": tier,
            "is_fallback": is_fallback,
        },
    )


def _fake_fetch_result():
    """Return a ToolResult-like object simulating a live browser.fetch response."""
    from core.models import ToolResult

    return ToolResult(
        summary="Fetched page",
        data={
            "content": "page text content",
            "title": "Test Title",
            "url": "https://example.com/a",
            "untrusted_content": {"risk": "none"},
        },
    )


def _fake_project_chunk():
    """Return a Citation-like object simulating a project source chunk."""
    from memory.source_retrieval import Citation

    return Citation(
        source_id=str(uuid.uuid4()),
        source_title="Project Doc",
        chunk_index=0,
        snippet="project snippet for testing",
        distance=0.1,
    )


def _fake_connector_chunk():
    """Return a Citation-like object simulating an indexed connector source chunk."""
    from memory.source_retrieval import Citation

    return Citation(
        source_id=str(uuid.uuid4()),
        source_title="Slack Thread",
        source_type="connector",
        chunk_index=0,
        snippet="connector synced snippet for testing",
        distance=0.2,
    )


def _fake_upload_chunk():
    """Return a Citation-like object simulating an indexed uploaded file."""
    from memory.source_retrieval import Citation

    return Citation(
        source_id=str(uuid.uuid4()),
        source_title="Uploaded Brief.pdf",
        source_type="upload",
        chunk_index=0,
        snippet="uploaded project evidence for testing",
        distance=0.15,
    )


async def _fake_tool_broker_execute(agent, tool: str, args: dict):
    """Fake tool broker: handles browser.search and browser.fetch."""
    if tool == "browser.search":
        return _fake_search_result()
    if tool == "browser.fetch":
        return _fake_fetch_result()
    from core.models import ToolResult
    return ToolResult(summary="ok", data={})


async def _fake_tool_broker_execute_degraded(agent, tool: str, args: dict):
    """Fake tool broker that returns degraded (fixture tier) search results."""
    if tool == "browser.search":
        return _fake_search_result(tier="fixture", is_fallback=True)
    if tool == "browser.fetch":
        return _fake_fetch_result()
    from core.models import ToolResult
    return ToolResult(summary="ok", data={})


@_requires_db
@pytest.mark.asyncio
async def test_run_lifecycle_completes_with_report_artifact():
    """run_research marks status='complete', saves a report artifact with ## Sources."""
    from core.models import Member
    from core.research import create_run, get_run

    member = Member(id=str(uuid.uuid4()), organization_id="default", email="t@t.com", role="user")
    run_id = await create_run(
        member,
        question="What is quantum entanglement?",
        source_scopes={"web": True},
    )

    with (
        patch("runtime.research_executor.complete_json", new=AsyncMock(return_value='{"queries": ["q1"]}')),
        patch("runtime.research_executor.complete_text", new=AsyncMock(return_value="Report citing [S1].")),
        patch("runtime.research_executor.tool_broker.execute", new=AsyncMock(side_effect=_fake_tool_broker_execute)),
        patch("runtime.research_executor.retrieve_source_chunks", new=AsyncMock(return_value=[])),
    ):
        from runtime.research_executor import run_research
        await run_research(run_id, "default")

    run = await get_run(run_id, "default")
    assert run is not None
    assert run["status"] == "complete"
    assert run["report_artifact_id"] is not None

    # Read content and check for ## Sources section
    from core.artifacts import read_artifact_content
    content_bytes = await read_artifact_content(run["report_artifact_id"])
    assert content_bytes is not None
    content = content_bytes.decode()
    assert "## Sources" in content
    assert "S1" in content

    # Prove persistence survives engine dispose + fresh connection
    from core.db import engine
    await engine.dispose()
    run2 = await get_run(run_id, "default")
    assert run2 is not None
    assert run2["status"] == "complete"
    assert run2["report_artifact_id"] == run["report_artifact_id"]


@_requires_db
@pytest.mark.asyncio
async def test_rerun_is_idempotent_no_duplicate_citations():
    """Re-running a run (e.g. via startup recovery) rebuilds clean evidence.

    Simulates an interrupted run that recovery re-executes: running run_research
    twice on the same run must NOT duplicate citations or collide [S#] markers.
    """
    from core.models import Member
    from core.research import create_run, list_citations, list_events

    member = Member(id=str(uuid.uuid4()), organization_id="default", email="t@t.com", role="user")
    run_id = await create_run(
        member,
        question="What is the speed of light?",
        source_scopes={"web": True},
    )

    patches = lambda: (  # noqa: E731 — small local helper for repeated patch context
        patch("runtime.research_executor.complete_json", new=AsyncMock(return_value='{"queries": ["q1"]}')),
        patch("runtime.research_executor.complete_text", new=AsyncMock(return_value="Report citing [S1].")),
        patch("runtime.research_executor.tool_broker.execute", new=AsyncMock(side_effect=_fake_tool_broker_execute)),
        patch("runtime.research_executor.retrieve_source_chunks", new=AsyncMock(return_value=[])),
    )

    from runtime.research_executor import run_research

    p1 = patches()
    with p1[0], p1[1], p1[2], p1[3]:
        await run_research(run_id, "default")
    first = await list_citations(run_id, "default")

    # Re-run the same run (what recovery does for an interrupted run).
    p2 = patches()
    with p2[0], p2[1], p2[2], p2[3]:
        await run_research(run_id, "default")
    second = await list_citations(run_id, "default")

    # Same count both times — not doubled.
    assert len(second) == len(first)
    # Markers are unique (no colliding S1/S1).
    markers = [c["marker"] for c in second]
    assert len(markers) == len(set(markers)), f"duplicate markers after re-run: {markers}"
    # Event log was also reset, not appended on top of the prior attempt.
    events = await list_events(run_id, "default")
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(set(seqs)), f"duplicate/non-monotonic event seqs: {seqs}"
    assert seqs[0] == 1


@_requires_db
@pytest.mark.asyncio
async def test_internal_external_merge():
    """With web + project scopes, list_citations returns both source types."""
    from core.models import Member
    from core.research import create_run, list_citations

    member = Member(id=str(uuid.uuid4()), organization_id="default", email="t@t.com", role="user")
    project_id = str(uuid.uuid4())  # no FK — random UUID is fine
    run_id = await create_run(
        member,
        question="Tell me about machine learning.",
        source_scopes={"web": True, "project": True},
        project_id=project_id,
    )

    with (
        patch("runtime.research_executor.complete_json", new=AsyncMock(return_value='{"queries": ["q1"]}')),
        patch("runtime.research_executor.complete_text", new=AsyncMock(return_value="Merged report.")),
        patch("runtime.research_executor.tool_broker.execute", new=AsyncMock(side_effect=_fake_tool_broker_execute)),
        patch("runtime.research_executor.retrieve_source_chunks", new=AsyncMock(return_value=[_fake_project_chunk()])),
    ):
        from runtime.research_executor import run_research
        await run_research(run_id, "default")

    citations = await list_citations(run_id, "default")
    source_types = {c["source_type"] for c in citations}
    assert "web" in source_types, f"Expected 'web' citation, got: {source_types}"
    assert "project" in source_types, f"Expected 'project' citation, got: {source_types}"


@_requires_db
@pytest.mark.asyncio
async def test_degraded_search_records_limitation():
    """Degraded (fixture/fallback) search: limitation recorded, no web citations created."""
    from core.models import Member
    from core.research import create_run, get_run, list_citations

    member = Member(id=str(uuid.uuid4()), organization_id="default", email="t@t.com", role="user")
    run_id = await create_run(
        member,
        question="History of the internet?",
        source_scopes={"web": True},
    )

    with (
        patch("runtime.research_executor.complete_json", new=AsyncMock(return_value='{"queries": ["q1"]}')),
        patch("runtime.research_executor.complete_text", new=AsyncMock(return_value="Degraded report.")),
        patch("runtime.research_executor.tool_broker.execute", new=AsyncMock(side_effect=_fake_tool_broker_execute_degraded)),
        patch("runtime.research_executor.retrieve_source_chunks", new=AsyncMock(return_value=[])),
    ):
        from runtime.research_executor import run_research
        await run_research(run_id, "default")

    run = await get_run(run_id, "default")
    assert run is not None
    assert run["status"] == "failed"
    assert run["report_artifact_id"] is None
    limitations = run.get("limitations") or ""
    assert "web search" in limitations.lower() or "unavailable" in limitations.lower(), (
        f"Expected web search limitation, got: {limitations!r}"
    )

    citations = await list_citations(run_id, "default")
    web_citations = [c for c in citations if c["source_type"] == "web"]
    assert web_citations == [], f"Expected no web citations for degraded search, got: {web_citations}"


def test_research_request_validates_source_and_citation_policy():
    from pydantic import ValidationError

    from routers.research import ResearchRequest

    default = ResearchRequest(question="  Valid research question  ")
    assert default.question == "Valid research question"
    assert default.source_scopes.web is True

    trusted = ResearchRequest(
        question="Trusted question",
        depth="trusted",
        source_scopes={"web": True, "allowed_domains": ["SEC.GOV", "sec.gov"]},
    )
    assert trusted.source_scopes.allowed_domains == ["sec.gov"]

    with pytest.raises(ValidationError, match="trusted research requires"):
        ResearchRequest(question="Trusted question", depth="trusted")
    with pytest.raises(ValidationError, match="invalid research domain"):
        ResearchRequest(
            question="Invalid domain",
            source_scopes={"web": True, "allowed_domains": ["https://example.com/path"]},
        )
    with pytest.raises(ValidationError, match="citation_policy"):
        ResearchRequest(question="Invalid policy", citation_policy="none")
    with pytest.raises(ValidationError, match="MCP research requires"):
        ResearchRequest(question="Missing MCP tool", source_scopes={"web": False, "mcp": True})

    mcp = ResearchRequest(
        question="Search the approved MCP source",
        source_scopes={
            "web": False,
            "mcp": True,
            "mcp_tools": [{
                "server_id": "server-1",
                "tool_name": "search_docs",
                "query_argument": "query",
                "arguments": {"limit": 3},
            }],
        },
    )
    assert mcp.source_scopes.mcp_tools[0].tool_name == "search_docs"


def test_research_domain_filter_includes_subdomains_and_honors_parent_block():
    from runtime.research_executor import _domain_matches

    assert _domain_matches("investor.example.com", "example.com") is True
    assert _domain_matches("notexample.com", "example.com") is False


@pytest.mark.asyncio
async def test_research_mcp_discovery_exposes_only_declared_read_tools(monkeypatch):
    from core.models import Member, ToolResult
    from routers import research as research_router

    async def allow(*_args, **_kwargs):
        return None

    async def discover(_agent, tool: str, args: dict):
        assert tool == "platform.actions"
        assert args == {"platform_id": "mcp:server-1"}
        return ToolResult(summary="tools", data={"actions": [
            {"name": "search", "annotations": {"readOnlyHint": True}},
            {"name": "send", "annotations": {"readOnlyHint": False}},
            {"name": "ambiguous", "annotations": {}},
        ]})

    monkeypatch.setattr(research_router.permissions, "check", allow)
    monkeypatch.setattr(research_router.tool_broker, "execute", discover)
    member = Member(id="member-1", organization_id="org-1", email="m@example.com", role="user")
    result = await research_router.list_research_mcp_tools("server-1", member=member)
    assert [action["name"] for action in result["actions"]] == ["search"]
    assert result["excluded_count"] == 2


@_requires_db
@pytest.mark.asyncio
async def test_uploaded_and_read_only_mcp_sources_create_real_citations():
    from core.models import Member, ToolResult
    from core.research import create_run, get_run, list_citations
    from runtime.research_executor import run_research

    member = Member(id=str(uuid.uuid4()), organization_id="default", email="t@t.com", role="user")
    upload_run = await create_run(
        member,
        question="What does the uploaded brief say?",
        source_scopes={"web": False, "upload": True},
        project_id=str(uuid.uuid4()),
    )
    with (
        patch("runtime.research_executor.complete_json", new=AsyncMock(return_value='{"queries": ["brief"]}')),
        patch("runtime.research_executor.complete_text", new=AsyncMock(return_value="Uploaded answer [S1].")),
        patch("runtime.research_executor.tool_broker.execute", new=AsyncMock(side_effect=_fake_tool_broker_execute)),
        patch("runtime.research_executor.retrieve_source_chunks", new=AsyncMock(return_value=[_fake_upload_chunk()])),
    ):
        await run_research(upload_run, "default")
    upload_citations = await list_citations(upload_run, "default")
    assert [citation["source_type"] for citation in upload_citations] == ["upload"]
    assert (await get_run(upload_run, "default"))["status"] == "complete"

    mcp_run = await create_run(
        member,
        question="What does the MCP knowledge source say?",
        source_scopes={
            "web": False,
            "mcp": True,
            "mcp_tools": [{
                "server_id": "server-1",
                "tool_name": "search_docs",
                "query_argument": "query",
                "arguments": {"limit": 3},
                "title": "Knowledge MCP",
            }],
        },
    )

    async def mcp_execute(_agent, tool: str, args: dict):
        if tool == "platform.actions":
            return ToolResult(summary="discovered", data={"actions": [{
                "name": "search_docs",
                "annotations": {"readOnlyHint": True},
                "parameters": {"type": "object"},
            }]})
        if tool == "mcp.server-1.search_docs":
            assert args == {"limit": 3, "query": "mcp evidence"}
            return ToolResult(
                summary="MCP result",
                data={
                    "result": {"documents": [{"title": "Policy", "text": "real MCP evidence"}]},
                    "untrusted_content": {"risk": "none"},
                },
            )
        return ToolResult(summary="unexpected", data={})

    with (
        patch("runtime.research_executor.complete_json", new=AsyncMock(return_value='{"queries": ["mcp evidence"]}')),
        patch("runtime.research_executor.complete_text", new=AsyncMock(return_value="MCP answer [S1].")),
        patch("runtime.research_executor.tool_broker.execute", new=AsyncMock(side_effect=mcp_execute)),
        patch("runtime.research_executor.retrieve_source_chunks", new=AsyncMock(return_value=[])),
    ):
        await run_research(mcp_run, "default")
    mcp_citations = await list_citations(mcp_run, "default")
    assert [citation["source_type"] for citation in mcp_citations] == ["mcp"]
    assert "real MCP evidence" in mcp_citations[0]["snippet"]
    assert (await get_run(mcp_run, "default"))["status"] == "complete"


@_requires_db
@pytest.mark.asyncio
async def test_cancel_stops_run():
    """Pre-cancelled run: run_research returns without changing status or creating artifact."""
    from core.models import Member
    from core.research import create_run, get_run, update_run

    member = Member(id=str(uuid.uuid4()), organization_id="default", email="t@t.com", role="user")
    run_id = await create_run(
        member,
        question="Cancelled research question?",
        source_scopes={"web": True},
    )

    # Cancel before execution
    await update_run(run_id, "default", status="cancelled")

    with (
        patch("runtime.research_executor.complete_json", new=AsyncMock(return_value='{"queries": ["q1"]}')),
        patch("runtime.research_executor.complete_text", new=AsyncMock(return_value="Should not be called.")),
        patch("runtime.research_executor.tool_broker.execute", new=AsyncMock(side_effect=_fake_tool_broker_execute)),
        patch("runtime.research_executor.retrieve_source_chunks", new=AsyncMock(return_value=[])),
    ):
        from runtime.research_executor import run_research
        await run_research(run_id, "default")

    run = await get_run(run_id, "default")
    assert run is not None
    assert run["status"] == "cancelled", f"Expected 'cancelled', got: {run['status']!r}"
    assert run["report_artifact_id"] is None


@_requires_db
@pytest.mark.asyncio
async def test_no_fabricated_connector_citation():
    """With connector scope enabled but no index, no connector citations are created."""
    from core.models import Member
    from core.research import create_run, list_citations, list_events

    member = Member(id=str(uuid.uuid4()), organization_id="default", email="t@t.com", role="user")
    run_id = await create_run(
        member,
        question="What connectors do we have?",
        source_scopes={"connector": True, "web": False},
    )

    with (
        patch("runtime.research_executor.complete_json", new=AsyncMock(return_value='{"queries": ["q1"]}')),
        patch("runtime.research_executor.complete_text", new=AsyncMock(return_value="No connector report.")),
        patch("runtime.research_executor.tool_broker.execute", new=AsyncMock(side_effect=_fake_tool_broker_execute)),
        patch("runtime.research_executor.retrieve_source_chunks", new=AsyncMock(return_value=[])),
    ):
        from runtime.research_executor import run_research
        await run_research(run_id, "default")

    # No connector citations
    citations = await list_citations(run_id, "default")
    connector_citations = [c for c in citations if c["source_type"] == "connector"]
    assert connector_citations == [], f"Expected no connector citations, got: {connector_citations}"

    # research_source_skipped event exists with scope="connector"
    events = await list_events(run_id, "default")
    skip_events = [
        e for e in events
        if e["event_type"] == "research_source_skipped"
        and (e.get("payload") or {}).get("scope") == "connector"
    ]
    assert skip_events, (
        f"Expected a research_source_skipped event for 'connector', "
        f"events: {[e['event_type'] for e in events]}"
    )


@_requires_db
@pytest.mark.asyncio
async def test_connector_indexed_sources_are_used_as_research_citations():
    """Connector scope uses indexed connector chunks and does not emit a skipped-source event."""
    from core.models import Member
    from core.research import create_run, list_citations, list_events

    member = Member(id=str(uuid.uuid4()), organization_id="default", email="t@t.com", role="user")
    run_id = await create_run(
        member,
        question="What did our Slack connector sync?",
        source_scopes={"connector": True, "web": False},
        project_id=str(uuid.uuid4()),
    )

    with (
        patch("runtime.research_executor.complete_json", new=AsyncMock(return_value='{"queries": ["q1"]}')),
        patch("runtime.research_executor.complete_text", new=AsyncMock(return_value="Connector report citing [S1].")),
        patch("runtime.research_executor.tool_broker.execute", new=AsyncMock(side_effect=_fake_tool_broker_execute)),
        patch("runtime.research_executor.retrieve_source_chunks", new=AsyncMock(return_value=[_fake_connector_chunk()])),
    ):
        from runtime.research_executor import run_research
        await run_research(run_id, "default")

    citations = await list_citations(run_id, "default")
    connector_citations = [c for c in citations if c["source_type"] == "connector"]
    assert len(connector_citations) == 1
    assert connector_citations[0]["snippet"] == "connector synced snippet for testing"

    events = await list_events(run_id, "default")
    assert not [
        e for e in events
        if e["event_type"] == "research_source_skipped"
        and (e.get("payload") or {}).get("scope") == "connector"
    ]


# ===========================================================================
# HTTP router tests (routers/research.py)
# ===========================================================================

import httpx


async def _seed_org_member_token() -> tuple[str, str, str]:
    """Seed a fresh org + member and return (org_id, member_id, jwt_token)."""
    from core.auth import create_access_token
    from core.db import engine, reflect_table

    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members_t = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(
            orgs.insert().values(id=org_id, slug=f"r-{org_id[:8]}", name="ResearchOrg")
        )
        await conn.execute(
            members_t.insert().values(
                id=member_id,
                organization_id=org_id,
                email=f"{member_id[:8]}@research.test",
                role="user",
            )
        )
    token = create_access_token(member_id)
    return org_id, member_id, token


@_requires_db
@pytest.mark.asyncio
async def test_create_and_get_run_http(monkeypatch):
    """POST /research/ creates a run; GET /research/{id} returns it with correct question."""
    import main

    started: list[str] = []

    async def fake_start(run_id: str, org_id: str) -> None:
        started.append(run_id)

    monkeypatch.setattr("routers.research.start_research", fake_start)

    org_id, _member_id, token = await _seed_org_member_token()
    auth = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Create
        resp = await client.post(
            "/research/",
            json={"question": "What is quantum computing?", "source_scopes": {"web": True}},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "run_id" in body
        assert body["status"] == "pending"
        run_id = body["run_id"]

        # start_research was called with this run_id
        assert run_id in started

        # Get
        get_resp = await client.get(f"/research/{run_id}", headers=auth)
        assert get_resp.status_code == 200, get_resp.text
        run = get_resp.json()
        assert run["id"] == run_id
        assert run["question"] == "What is quantum computing?"
        assert run["status"] == "pending"


@_requires_db
@pytest.mark.asyncio
async def test_create_run_rejects_bad_depth(monkeypatch):
    """POST /research/ with depth='ultra' returns 422."""
    import main

    async def fake_start(run_id: str, org_id: str) -> None:
        pass

    monkeypatch.setattr("routers.research.start_research", fake_start)

    _org_id, _member_id, token = await _seed_org_member_token()
    auth = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/research/",
            json={"question": "Some question", "depth": "ultra"},
            headers=auth,
        )
        assert resp.status_code == 422, resp.text


@_requires_db
@pytest.mark.asyncio
async def test_run_cross_org_404(monkeypatch):
    """A run created by org A is invisible (404) to a member of org B."""
    import main

    async def fake_start(run_id: str, org_id: str) -> None:
        pass

    monkeypatch.setattr("routers.research.start_research", fake_start)

    # Seed org A and create a run
    _org_a, _m_a, token_a = await _seed_org_member_token()
    _org_b, _m_b, token_b = await _seed_org_member_token()

    auth_a = {"Authorization": f"Bearer {token_a}"}
    auth_b = {"Authorization": f"Bearer {token_b}"}
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Create run as org A
        resp = await client.post(
            "/research/",
            json={"question": "Org A question?"},
            headers=auth_a,
        )
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["run_id"]

        # Org B cannot see it
        cross = await client.get(f"/research/{run_id}", headers=auth_b)
        assert cross.status_code == 404, cross.text


@_requires_db
@pytest.mark.asyncio
async def test_cancel_sets_status(monkeypatch):
    """Cancel endpoint sets status to 'cancelled'; second cancel returns cancelled=False."""
    import main

    async def fake_start(run_id: str, org_id: str) -> None:
        pass

    monkeypatch.setattr("routers.research.start_research", fake_start)

    _org_id, _member_id, token = await _seed_org_member_token()
    auth = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Create
        create_resp = await client.post(
            "/research/",
            json={"question": "Cancellable question?"},
            headers=auth,
        )
        assert create_resp.status_code == 200, create_resp.text
        run_id = create_resp.json()["run_id"]

        # First cancel
        cancel_resp = await client.post(f"/research/{run_id}/cancel", headers=auth)
        assert cancel_resp.status_code == 200, cancel_resp.text
        cancel_body = cancel_resp.json()
        assert cancel_body["cancelled"] is True
        assert cancel_body["status"] == "cancelled"

        # Verify persisted
        get_resp = await client.get(f"/research/{run_id}", headers=auth)
        assert get_resp.json()["status"] == "cancelled"

        # Second cancel is a no-op
        cancel_resp2 = await client.post(f"/research/{run_id}/cancel", headers=auth)
        assert cancel_resp2.status_code == 200, cancel_resp2.text
        assert cancel_resp2.json()["cancelled"] is False


@_requires_db
@pytest.mark.asyncio
async def test_recover_incomplete_research_reenqueues(monkeypatch):
    """recover_incomplete_research re-enqueues pending runs via start_research."""
    import main

    requeued: list[str] = []

    async def fake_start(run_id: str, org_id: str) -> None:
        requeued.append(run_id)

    monkeypatch.setattr("main.start_research", fake_start)

    # Insert a research_runs row directly with status "pending"
    from core.db import engine, reflect_table

    run_id = str(uuid.uuid4())
    org_id = "default"
    table = await reflect_table("research_runs")
    async with engine.begin() as conn:
        await conn.execute(
            table.insert().values(
                id=run_id,
                organization_id=org_id,
                region="us",
                member_id=str(uuid.uuid4()),
                question="Recovery test question?",
                depth="standard",
                source_scopes={},
                citation_policy="required",
                status="pending",
            )
        )

    recovered = await main.recover_incomplete_research()

    assert run_id in recovered
    assert run_id in requeued
