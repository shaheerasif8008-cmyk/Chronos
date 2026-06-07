"""Global search endpoint tests.

Pattern: direct import + monkeypatch (no TestClient, no DB) except for the
org-isolation test which uses a real in-memory SQLite engine so the assertion
is structural (not just vacuously true for an empty result set).

Five key assertions:
  1. Org-isolation — rows from another org never appear.
  2. types= filter — narrows to requested types.
  3. Memory path uses the seam (memory.retrieve), NOT a raw query.
  4. Missing project_sources table degrades gracefully (no 500).
  5. Permission check and audit fire before queries.

New assertions added in review pass:
  6. Empty q= returns 422 at the HTTP boundary.
  7. LIKE metacharacters are escaped in the ilike pattern.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_member(org_id: str = "default", member_id: str = "member-1"):
    from core.models import Member
    return Member(id=member_id, organization_id=org_id, email="admin@test.com", role="owner")


def _make_context(member):
    from core.models import RequesterContext
    return RequesterContext.from_member(member)


def _fake_mapping(rows: list[dict]):
    """Return an object whose .mappings().all() returns rows."""
    result = MagicMock()
    result.mappings.return_value.all.return_value = [
        MagicMock(**{"__getitem__": lambda s, k: row[k], "keys": lambda s: row.keys(), **row})
        for row in rows
    ]
    return result


class _FakeTable:
    """Minimal reflected-table mock exposing .c.<column> attributes."""

    def __init__(self, columns: list[str]):
        self.c = MagicMock()
        for col in columns:
            col_obj = MagicMock()
            col_obj.__eq__ = lambda self, other, _c=col: ("eq", _c, other)  # type: ignore[assignment]
            col_obj.ilike = lambda pattern, _c=col, **kwargs: ("ilike", _c, pattern, kwargs)
            col_obj.desc = lambda _c=col: ("desc", _c)
            col_obj.in_ = lambda vals, _c=col: ("in_", _c, vals)
            setattr(self.c, col, col_obj)


def _fake_conn(rows_by_table: dict[str, list[dict]]):
    """Context manager conn that returns rows based on table name in the statement."""

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def execute(self, stmt, *args, **kwargs):
            return _fake_mapping([])

    return FakeConn()


# ─── Test 1: org-isolation (load-bearing with real SQLite engine) ─────────────

@pytest.mark.asyncio
async def test_search_returns_only_caller_org_rows(monkeypatch):
    """Rows belonging to a different org must never appear in results.

    Uses a real async SQLite engine so the WHERE filters are actually evaluated.
    Inserts rows for both "default" and "other-org"; asserts the "other-org"
    rows never surface regardless of which handler is called.
    """
    from routers import search

    # ── Build an in-memory SQLite engine with the minimal schema ──────────────
    sqlite_engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)

    meta = sa.MetaData()
    conversations_tbl = sa.Table(
        "conversations", meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("member_id", sa.String, nullable=False),
        sa.Column("title", sa.String),
        sa.Column("created_at", sa.String),
        sa.Column("updated_at", sa.String),
    )
    messages_tbl = sa.Table(
        "messages", meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("conversation_id", sa.String, nullable=False),
        sa.Column("content", sa.String),
        sa.Column("created_at", sa.String),
    )
    tasks_tbl = sa.Table(
        "tasks", meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("goal", sa.String),
        sa.Column("status", sa.String),
        sa.Column("created_at", sa.String),
    )
    artifacts_tbl = sa.Table(
        "artifacts", meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("title", sa.String),
        sa.Column("kind", sa.String),
        sa.Column("created_at", sa.String),
    )

    async with sqlite_engine.begin() as conn:
        await conn.run_sync(meta.create_all)
        # Insert one matching row per table for the CALLER org "default"
        await conn.execute(conversations_tbl.insert().values(
            id="conv-default", organization_id="default", member_id="member-1",
            title="acme project", created_at="2024-01-01T00:00:00Z", updated_at="2024-01-01T00:00:00Z",
        ))
        await conn.execute(messages_tbl.insert().values(
            id="msg-default", organization_id="default", conversation_id="conv-default",
            content="acme account notes", created_at="2024-01-01T00:00:00Z",
        ))
        await conn.execute(tasks_tbl.insert().values(
            id="task-default", organization_id="default",
            goal="research acme", status="complete", created_at="2024-01-01T00:00:00Z",
        ))
        await conn.execute(artifacts_tbl.insert().values(
            id="art-default", organization_id="default",
            title="acme brief", kind="document", created_at="2024-01-01T00:00:00Z",
        ))
        # Insert matching rows for a DIFFERENT org — these must NEVER appear.
        await conn.execute(conversations_tbl.insert().values(
            id="conv-other", organization_id="other-org", member_id="member-x",
            title="acme other org", created_at="2024-01-01T00:00:00Z", updated_at="2024-01-01T00:00:00Z",
        ))
        await conn.execute(messages_tbl.insert().values(
            id="msg-other", organization_id="other-org", conversation_id="conv-other",
            content="acme stolen data", created_at="2024-01-01T00:00:00Z",
        ))
        await conn.execute(tasks_tbl.insert().values(
            id="task-other", organization_id="other-org",
            goal="acme other goal", status="complete", created_at="2024-01-01T00:00:00Z",
        ))
        await conn.execute(artifacts_tbl.insert().values(
            id="art-other", organization_id="other-org",
            title="acme other artifact", kind="document", created_at="2024-01-01T00:00:00Z",
        ))

    # ── Monkeypatch search module to use the in-memory engine ─────────────────
    TABLE_MAP = {
        "conversations": conversations_tbl,
        "messages": messages_tbl,
        "tasks": tasks_tbl,
        "artifacts": artifacts_tbl,
    }

    async def fake_reflect_table(name: str):
        if name not in TABLE_MAP:
            raise Exception(f"Table {name!r} does not exist")
        return TABLE_MAP[name]

    async def fake_permissions_check(*args, **kwargs):
        return True

    async def fake_audit_log(*args, **kwargs):
        return "audit-1"

    async def fake_memory_retrieve(query, requester_context):
        assert requester_context.org_id == "default"
        return []

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search, "engine", sqlite_engine)
    monkeypatch.setattr(search.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(search.audit, "log", fake_audit_log)
    monkeypatch.setattr(search.memory, "retrieve", fake_memory_retrieve)

    member = _make_member(org_id="default", member_id="member-1")
    results = await search.run_search(q="acme", types_csv="conversations,messages,tasks,artifacts", member=member)

    # Must have at least one hit (proves the query ran for real)
    assert results, "Expected at least one search hit from 'default' org, got none"

    # Forbidden: no row from the other org must slip through
    leaked_ids = {
        "conv-other", "msg-other", "task-other", "art-other",
    }
    returned_ids = {r["id"] for r in results}
    assert not (returned_ids & leaked_ids), (
        f"Org-isolation breach: other-org rows appeared in results: "
        f"{returned_ids & leaked_ids}"
    )

    # Confirm we got exactly the default-org rows
    expected_ids = {"conv-default", "msg-default", "task-default", "art-default"}
    assert returned_ids == expected_ids, (
        f"Expected default-org rows {expected_ids}, got {returned_ids}"
    )

    await sqlite_engine.dispose()


# ─── Test 2: types filter narrows results ────────────────────────────────────

@pytest.mark.asyncio
async def test_search_types_filter_only_queries_requested_types(monkeypatch):
    """When types=conversations, only conversations are queried; memory.retrieve not called."""
    from routers import search

    member = _make_member()

    conversations_table = _FakeTable(["id", "title", "member_id", "organization_id", "created_at", "updated_at"])
    queried_tables: list[str] = []

    async def fake_reflect_table(name: str):
        queried_tables.append(name)
        if name == "conversations":
            return conversations_table
        raise Exception(f"Unexpected reflect_table({name!r}) for types=conversations test")

    class FakeConn:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return None
        async def execute(self, stmt, *args, **kwargs):
            return _fake_mapping([])

    class FakeEngine:
        def begin(self):
            return FakeConn()

    memory_called = []

    async def fake_memory_retrieve(query, requester_context):
        memory_called.append(True)
        return []

    async def fake_permissions_check(*args, **kwargs):
        return True

    async def fake_audit_log(*args, **kwargs):
        return "audit-1"

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search, "engine", FakeEngine())
    monkeypatch.setattr(search.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(search.audit, "log", fake_audit_log)
    monkeypatch.setattr(search.memory, "retrieve", fake_memory_retrieve)

    results = await search.run_search(q="hello", types_csv="conversations", member=member)

    # memory.retrieve must NOT be called when types=conversations
    assert not memory_called, "memory.retrieve was called despite types=conversations"
    # Only conversations table should have been reflected
    assert "conversations" in queried_tables
    assert "messages" not in queried_tables
    assert "tasks" not in queried_tables
    assert "artifacts" not in queried_tables


# ─── Test 3: memory path uses the seam, not raw SQL ──────────────────────────

@pytest.mark.asyncio
async def test_search_memory_type_uses_retrieve_seam_not_raw_query(monkeypatch):
    """When types=memory, memory.retrieve is called; no raw memory_entries query."""
    from core.models import MemoryEntry
    from routers import search

    member = _make_member()

    reflected_tables: list[str] = []

    async def fake_reflect_table(name: str):
        # If search.py tries to reflect memory_entries, this list will include it — fail.
        reflected_tables.append(name)
        raise Exception(f"Unexpected reflect_table({name!r}) for types=memory test")

    memory_retrieve_calls: list = []

    async def fake_memory_retrieve(query, requester_context):
        memory_retrieve_calls.append({"query": query, "ctx": requester_context})
        return [
            MemoryEntry(
                id="mem-1",
                organization_id="default",
                content="ACME uses HubSpot.",
                scope="org",
                scope_id="default",
                source="explicit",
            )
        ]

    async def fake_permissions_check(*args, **kwargs):
        return True

    async def fake_audit_log(*args, **kwargs):
        return "audit-1"

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(search.audit, "log", fake_audit_log)
    monkeypatch.setattr(search.memory, "retrieve", fake_memory_retrieve)

    results = await search.run_search(q="hubspot", types_csv="memory", member=member)

    # Seam must have been called
    assert len(memory_retrieve_calls) == 1
    assert memory_retrieve_calls[0]["query"] == "hubspot"
    # memory_entries table must NOT have been reflected directly
    assert "memory_entries" not in reflected_tables
    # Result contains the memory hit
    memory_hits = [r for r in results if r["type"] == "memory"]
    assert len(memory_hits) == 1
    assert "ACME" in memory_hits[0]["snippet"]


# ─── Test 4: missing project_sources degrades gracefully ─────────────────────

@pytest.mark.asyncio
async def test_search_missing_project_sources_returns_empty_not_500(monkeypatch):
    """If project_sources table doesn't exist, sources yields [] — no exception."""
    from routers import search

    member = _make_member()

    async def fake_reflect_table(name: str):
        if name == "project_sources":
            raise Exception("relation 'project_sources' does not exist")
        raise Exception(f"Unexpected reflect_table({name!r}) for sources-only test")

    async def fake_permissions_check(*args, **kwargs):
        return True

    async def fake_audit_log(*args, **kwargs):
        return "audit-1"

    async def fake_memory_retrieve(query, requester_context):
        return []

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(search.audit, "log", fake_audit_log)
    monkeypatch.setattr(search.memory, "retrieve", fake_memory_retrieve)

    # Must not raise
    results = await search.run_search(q="contract", types_csv="sources", member=member)

    # Sources degrade to empty — no 500
    source_hits = [r for r in results if r["type"] == "sources"]
    assert source_hits == []


# ─── Test 5: permission check and audit fire before queries ──────────────────

@pytest.mark.asyncio
async def test_search_permission_and_audit_fire_on_every_call(monkeypatch):
    """permission.check and audit.log must be called even when q matches nothing."""
    from routers import search

    member = _make_member()

    perm_calls: list = []
    audit_calls: list = []

    async def fake_reflect_table(name: str):
        table = _FakeTable(["id", "title", "member_id", "organization_id", "created_at", "updated_at",
                             "goal", "content", "conversation_id", "created_at"])
        return table

    class FakeConn:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return None
        async def execute(self, stmt, *args, **kwargs):
            return _fake_mapping([])

    class FakeEngine:
        def begin(self):
            return FakeConn()

    async def fake_permissions_check(actor, action, resource):
        perm_calls.append((action, resource))
        return True

    async def fake_audit_log(event_type, actor_id, action, **kwargs):
        audit_calls.append((event_type, action))
        return "audit-1"

    async def fake_memory_retrieve(query, requester_context):
        return []

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search, "engine", FakeEngine())
    monkeypatch.setattr(search.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(search.audit, "log", fake_audit_log)
    monkeypatch.setattr(search.memory, "retrieve", fake_memory_retrieve)

    await search.run_search(q="anything", types_csv=None, member=member)

    assert any(a == "search" for a, _ in perm_calls), "permission.check('search', ...) not called"
    assert any(e == "search" for e, _ in audit_calls), "audit.log('search', ...) not called"


# ─── Test 6: empty q= returns 422 at the HTTP boundary ───────────────────────

def test_search_empty_q_returns_422():
    """GET /search?q= (empty string) must return HTTP 422 — rejected by FastAPI validator."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from core.auth import get_current_member
    from core.models import Member
    from routers.search import router

    app = FastAPI()
    app.include_router(router)

    # Override auth so the request reaches FastAPI's query-param validator
    def fake_member():
        return Member(id="member-1", organization_id="default", email="admin@test.com", role="owner")
    app.dependency_overrides[get_current_member] = fake_member

    client = TestClient(app, raise_server_exceptions=False)
    # Empty q should be rejected before reaching any handler
    resp = client.get("/search?q=")
    assert resp.status_code == 422, (
        f"Expected 422 for empty q, got {resp.status_code}: {resp.text}"
    )


# ─── Test 7: LIKE metacharacters are escaped ─────────────────────────────────

@pytest.mark.asyncio
async def test_search_like_metacharacters_escaped(monkeypatch):
    """q='50%' must search for the literal string '50%', not 50-followed-by-wildcard.

    Uses a real in-memory SQLite engine so we can verify that a row containing
    the literal text '50%' is found AND that a row containing '50x' (which
    would match if % were treated as a wildcard) is NOT returned.
    """
    from routers import search

    sqlite_engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)

    meta = sa.MetaData()
    conversations_tbl = sa.Table(
        "conversations", meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("member_id", sa.String, nullable=False),
        sa.Column("title", sa.String),
        sa.Column("created_at", sa.String),
        sa.Column("updated_at", sa.String),
    )

    async with sqlite_engine.begin() as conn:
        await conn.run_sync(meta.create_all)
        # This row SHOULD match (literal "50%" in title)
        await conn.execute(conversations_tbl.insert().values(
            id="conv-pct", organization_id="default", member_id="member-1",
            title="discount 50% off", created_at="2024-01-01T00:00:00Z", updated_at="2024-01-01T00:00:00Z",
        ))
        # This row must NOT match (has "50x", only matches if % is unescaped wildcard)
        await conn.execute(conversations_tbl.insert().values(
            id="conv-other", organization_id="default", member_id="member-1",
            title="discount 50x off", created_at="2024-01-01T00:00:00Z", updated_at="2024-01-01T00:00:00Z",
        ))

    async def fake_reflect_table(name: str):
        if name == "conversations":
            return conversations_tbl
        raise Exception(f"Unexpected reflect_table({name!r})")

    async def fake_permissions_check(*args, **kwargs):
        return True

    async def fake_audit_log(*args, **kwargs):
        return "audit-1"

    async def fake_memory_retrieve(query, requester_context):
        return []

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search, "engine", sqlite_engine)
    monkeypatch.setattr(search.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(search.audit, "log", fake_audit_log)
    monkeypatch.setattr(search.memory, "retrieve", fake_memory_retrieve)

    member = _make_member(org_id="default", member_id="member-1")
    results = await search.run_search(q="50%", types_csv="conversations", member=member)

    returned_ids = {r["id"] for r in results}
    assert "conv-pct" in returned_ids, (
        "Expected row with literal '50%' to be found, but it was not"
    )
    assert "conv-other" not in returned_ids, (
        "Row '50x' matched when searching for '50%' — metacharacter not escaped"
    )

    await sqlite_engine.dispose()
