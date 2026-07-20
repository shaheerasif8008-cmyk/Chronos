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
        sa.Column("project_id", sa.String),
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
        sa.Column("triggered_by_member_id", sa.String),
        sa.Column("project_id", sa.String),
        sa.Column("goal", sa.String),
        sa.Column("status", sa.String),
        sa.Column("created_at", sa.String),
    )
    artifacts_tbl = sa.Table(
        "artifacts", meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("created_by", sa.String),
        sa.Column("is_deleted", sa.Boolean, nullable=False, default=False),
        sa.Column("project_id", sa.String),
        sa.Column("conversation_id", sa.String),
        sa.Column("task_id", sa.String),
        sa.Column("title", sa.String),
        sa.Column("kind", sa.String),
        sa.Column("created_at", sa.String),
    )
    project_members_tbl = sa.Table(
        "project_members", meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("member_id", sa.String, nullable=False),
        sa.Column("project_id", sa.String, nullable=False),
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
            id="task-default", organization_id="default", triggered_by_member_id="member-1",
            goal="research acme", status="complete", created_at="2024-01-01T00:00:00Z",
        ))
        await conn.execute(artifacts_tbl.insert().values(
            id="art-default", organization_id="default", created_by="member:member-1", is_deleted=False,
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
            id="task-other", organization_id="other-org", triggered_by_member_id="member-x",
            goal="acme other goal", status="complete", created_at="2024-01-01T00:00:00Z",
        ))
        await conn.execute(artifacts_tbl.insert().values(
            id="art-other", organization_id="other-org", created_by="member:member-x", is_deleted=False,
            title="acme other artifact", kind="document", created_at="2024-01-01T00:00:00Z",
        ))

    # ── Monkeypatch search module to use the in-memory engine ─────────────────
    TABLE_MAP = {
        "conversations": conversations_tbl,
        "messages": messages_tbl,
        "tasks": tasks_tbl,
        "artifacts": artifacts_tbl,
        "project_members": project_members_tbl,
    }

    async def fake_reflect_table(name: str):
        if name not in TABLE_MAP:
            raise Exception(f"Table {name!r} does not exist")
        return TABLE_MAP[name]

    async def fake_permissions_check(*args, **kwargs):
        return True

    async def fake_audit_log(*args, **kwargs):
        return "audit-1"

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search, "engine", sqlite_engine)
    monkeypatch.setattr(search.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(search.audit, "log", fake_audit_log)

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

    async def fake_permissions_check(*args, **kwargs):
        return True

    async def fake_audit_log(*args, **kwargs):
        return "audit-1"

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search, "engine", FakeEngine())
    monkeypatch.setattr(search.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(search.audit, "log", fake_audit_log)

    results = await search.run_search(q="hello", types_csv="conversations", member=member)

    # Only conversations table should have been reflected
    assert "conversations" in queried_tables
    assert "messages" not in queried_tables
    assert "tasks" not in queried_tables
    assert "artifacts" not in queried_tables


# ─── Test 3: memory path uses the seam, not raw SQL ──────────────────────────

@pytest.mark.asyncio
async def test_search_memory_type_uses_canonical_all_scope_acl(monkeypatch):
    """Global memory search uses the control-center ACL across all readable scopes."""
    from routers import search

    member = _make_member()
    sqlite_engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    metadata = sa.MetaData()
    table = sa.Table(
        "memory_entries", metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("scope", sa.String, nullable=False),
        sa.Column("scope_id", sa.String, nullable=False),
        sa.Column("content", sa.String, nullable=False),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("is_deleted", sa.Boolean, nullable=False),
        sa.Column("is_archived", sa.Boolean, nullable=False),
        sa.Column("is_pinned", sa.Boolean, nullable=False),
        sa.Column("superseded_by", sa.String),
        sa.Column("created_at", sa.String),
        sa.Column("updated_at", sa.String),
    )
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.execute(table.insert(), [
            {
                "id": "mem-1", "organization_id": "default", "scope": "personal", "scope_id": member.id,
                "content": "ACME uses HubSpot.", "source": "explicit", "is_deleted": False,
                "is_archived": False, "is_pinned": False, "superseded_by": None,
                "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
            },
            {
                "id": "mem-peer", "organization_id": "default", "scope": "personal", "scope_id": "member-peer",
                "content": "Peer HubSpot secret.", "source": "explicit", "is_deleted": False,
                "is_archived": False, "is_pinned": False, "superseded_by": None,
                "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
            },
        ])

    async def fake_reflect_table(name: str):
        assert name == "memory_entries"
        return table

    async def allow_visible(table_arg, member_arg):
        assert table_arg is table and member_arg is member
        return table_arg.c.scope_id == member_arg.id

    async def fake_permissions_check(*args, **kwargs):
        return True

    async def fake_audit_log(*args, **kwargs):
        return "audit-1"

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search, "memory_access_condition", allow_visible)
    monkeypatch.setattr(search, "engine", sqlite_engine)
    monkeypatch.setattr(search.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(search.audit, "log", fake_audit_log)

    results = await search.run_search(q="hubspot", types_csv="memory", member=member)

    memory_hits = [r for r in results if r["type"] == "memory"]
    assert len(memory_hits) == 1
    assert "ACME" in memory_hits[0]["snippet"]
    assert memory_hits[0]["id"] == "mem-1"
    await sqlite_engine.dispose()


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

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(search.audit, "log", fake_audit_log)

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

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search, "engine", FakeEngine())
    monkeypatch.setattr(search.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(search.audit, "log", fake_audit_log)

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

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search, "engine", sqlite_engine)
    monkeypatch.setattr(search.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(search.audit, "log", fake_audit_log)

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


# ─── Test 8: sources are scoped to project membership (not just org) ───────────

@pytest.mark.asyncio
async def test_search_sources_scoped_to_project_membership(monkeypatch):
    """A project source must only surface for members of that project.

    Seeds two projects in the SAME org: project-A (caller is a member) and
    project-B (caller is NOT a member). Both have a source whose title matches
    the query. Only project-A's source may be returned — an org peer outside a
    project must not see that project's sources via search.
    """
    from routers import search

    sqlite_engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    meta = sa.MetaData()
    project_sources_tbl = sa.Table(
        "project_sources", meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("project_id", sa.String, nullable=False),
        sa.Column("title", sa.String),
        sa.Column("permissions", sa.JSON, nullable=False, default=dict),
        sa.Column("created_by", sa.String),
        sa.Column("index_status", sa.String, nullable=False, default="indexed"),
        sa.Column("created_at", sa.String),
    )
    project_source_chunks_tbl = sa.Table(
        "project_source_chunks", meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("project_id", sa.String, nullable=False),
        sa.Column("source_id", sa.String, nullable=False),
        sa.Column("content", sa.String, nullable=False),
    )
    project_members_tbl = sa.Table(
        "project_members", meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("project_id", sa.String, nullable=False),
        sa.Column("member_id", sa.String, nullable=False),
        sa.Column("role", sa.String, nullable=False, default="member"),
    )

    async with sqlite_engine.begin() as conn:
        await conn.run_sync(meta.create_all)
        # Caller (member-1) belongs to project-A only.
        await conn.execute(project_members_tbl.insert().values(
            id="pm-1", organization_id="default", project_id="project-A", member_id="member-1", role="member",
        ))
        await conn.execute(project_sources_tbl.insert().values(
            id="src-A", organization_id="default", project_id="project-A",
            title="acme handbook", permissions={}, created_by="member-1", index_status="indexed",
            created_at="2024-01-02T00:00:00Z",
        ))
        # project-B is in the same org but the caller is NOT a member.
        await conn.execute(project_sources_tbl.insert().values(
            id="src-B", organization_id="default", project_id="project-B",
            title="acme secrets", permissions={}, created_by="member-x", index_status="indexed",
            created_at="2024-01-03T00:00:00Z",
        ))
        await conn.execute(project_sources_tbl.insert(), [
            {
                "id": "src-private", "organization_id": "default", "project_id": "project-A",
                "title": "private notes", "permissions": {"visibility": "private"},
                "created_by": "member-x", "index_status": "indexed", "created_at": "2024-01-04T00:00:00Z",
            },
            {
                "id": "src-revoked", "organization_id": "default", "project_id": "project-A",
                "title": "revoked notes", "permissions": {"revoked": True},
                "created_by": "member-1", "index_status": "indexed", "created_at": "2024-01-05T00:00:00Z",
            },
        ])
        await conn.execute(project_source_chunks_tbl.insert(), [
            {"id": "chunk-a", "organization_id": "default", "project_id": "project-A", "source_id": "src-A", "content": "content-only-needle"},
            {"id": "chunk-private", "organization_id": "default", "project_id": "project-A", "source_id": "src-private", "content": "content-only-needle"},
            {"id": "chunk-revoked", "organization_id": "default", "project_id": "project-A", "source_id": "src-revoked", "content": "content-only-needle"},
        ])

    TABLE_MAP = {
        "project_sources": project_sources_tbl,
        "project_source_chunks": project_source_chunks_tbl,
        "project_members": project_members_tbl,
    }

    async def fake_reflect_table(name: str):
        if name not in TABLE_MAP:
            raise Exception(f"Table {name!r} does not exist")
        return TABLE_MAP[name]

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search, "engine", sqlite_engine)
    monkeypatch.setattr(search.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(search.audit, "log", AsyncMock(return_value="audit-1"))

    member = _make_member(org_id="default", member_id="member-1")
    results = await search.run_search(q="acme", types_csv="sources", member=member)

    returned_ids = {r["id"] for r in results}
    assert "src-A" in returned_ids, "Member's own project source should be found"
    assert "src-B" not in returned_ids, (
        "Authorization breach: a non-member saw project-B's source via search"
    )

    content_results = await search.run_search(
        q="content-only-needle", types_csv="sources", member=member
    )
    content_ids = {row["id"] for row in content_results}
    assert content_ids == {"src-A"}, (
        "Chunk-only search must return the authorized source while excluding "
        "private and revoked documents."
    )

    await sqlite_engine.dispose()


# ─── Test 9: results are relevance-ranked (title match outranks body match) ────

@pytest.mark.asyncio
async def test_search_results_are_relevance_ranked(monkeypatch):
    """An exact title match must rank above a body-only (snippet) match."""
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
    messages_tbl = sa.Table(
        "messages", meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("conversation_id", sa.String, nullable=False),
        sa.Column("content", sa.String),
        sa.Column("created_at", sa.String),
    )

    async with sqlite_engine.begin() as conn:
        await conn.run_sync(meta.create_all)
        # Exact-title conversation match for "acme".
        await conn.execute(conversations_tbl.insert().values(
            id="conv-exact", organization_id="default", member_id="member-1",
            title="acme", created_at="2024-01-01T00:00:00Z", updated_at="2024-01-01T00:00:00Z",
        ))
        # Message whose body merely contains "acme" (lower relevance).
        await conn.execute(conversations_tbl.insert().values(
            id="conv-host", organization_id="default", member_id="member-1",
            title="quarterly planning", created_at="2024-01-09T00:00:00Z", updated_at="2024-01-09T00:00:00Z",
        ))
        await conn.execute(messages_tbl.insert().values(
            id="msg-body", organization_id="default", conversation_id="conv-host",
            content="we should reach out to acme next week", created_at="2024-01-09T00:00:00Z",
        ))

    TABLE_MAP = {"conversations": conversations_tbl, "messages": messages_tbl}

    async def fake_reflect_table(name: str):
        if name not in TABLE_MAP:
            raise Exception(f"Table {name!r} does not exist")
        return TABLE_MAP[name]

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search, "engine", sqlite_engine)
    monkeypatch.setattr(search.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(search.audit, "log", AsyncMock(return_value="audit-1"))

    member = _make_member(org_id="default", member_id="member-1")
    results = await search.run_search(q="acme", types_csv="conversations,messages", member=member)

    ids = [r["id"] for r in results]
    assert ids[0] == "conv-exact", f"Exact title match should rank first, got order {ids}"
    assert "msg-body" in ids
    assert ids.index("conv-exact") < ids.index("msg-body")

    # limit caps the ranked set.
    limited = await search.run_search(
        q="acme", types_csv="conversations,messages", member=member, limit=1
    )
    assert len(limited) == 1 and limited[0]["id"] == "conv-exact"

    await sqlite_engine.dispose()
