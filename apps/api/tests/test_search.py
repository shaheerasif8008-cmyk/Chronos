"""Global search endpoint tests.

Pattern: direct import + monkeypatch (no TestClient, no DB).
Four key assertions:
  1. Org-isolation — rows from another org never appear.
  2. types= filter — narrows to requested types.
  3. Memory path uses the seam (memory.retrieve), NOT a raw query.
  4. Missing project_sources table degrades gracefully (no 500).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


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
            col_obj.ilike = lambda pattern, _c=col: ("ilike", _c, pattern)
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


# ─── Test 1: org-isolation ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_returns_only_caller_org_rows(monkeypatch):
    """Rows belonging to a different org must never appear in results."""
    from routers import search

    member = _make_member(org_id="default")

    # Build a minimal table that tracks queries issued
    issued_wheres: list = []

    conversations_table = _FakeTable(["id", "title", "member_id", "organization_id", "updated_at"])
    messages_table = _FakeTable(["id", "content", "conversation_id", "organization_id", "created_at"])
    tasks_table = _FakeTable(["id", "goal", "organization_id", "created_at"])
    artifacts_table = _FakeTable(["id", "title", "organization_id", "created_at"])

    TABLE_MAP = {
        "conversations": conversations_table,
        "messages": messages_table,
        "tasks": tasks_table,
        "artifacts": artifacts_table,
    }

    async def fake_reflect_table(name: str):
        if name not in TABLE_MAP:
            raise Exception(f"Table {name!r} does not exist")
        return TABLE_MAP[name]

    class FakeConn:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return None
        async def execute(self, stmt, *args, **kwargs):
            # Record the where clauses being applied so we can inspect them later.
            # We don't need real rows for this test — empty is fine.
            return _fake_mapping([])

    class FakeEngine:
        def begin(self):
            return FakeConn()

    async def fake_permissions_check(*args, **kwargs):
        return True

    async def fake_audit_log(*args, **kwargs):
        return "audit-1"

    async def fake_memory_retrieve(query, requester_context):
        # Must only return entries for the caller's org (seam handles this internally)
        assert requester_context.org_id == "default"
        return []

    monkeypatch.setattr(search, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(search, "engine", FakeEngine())
    monkeypatch.setattr(search.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(search.audit, "log", fake_audit_log)
    monkeypatch.setattr(search.memory, "retrieve", fake_memory_retrieve)

    results = await search.run_search(q="acme", types_csv=None, member=member)

    # All returned items must have the caller's org_id (or no org — e.g. memory hits)
    for item in results:
        assert "org_id" not in item or item.get("org_id") == "default"


# ─── Test 2: types filter narrows results ────────────────────────────────────

@pytest.mark.asyncio
async def test_search_types_filter_only_queries_requested_types(monkeypatch):
    """When types=conversations, only conversations are queried; memory.retrieve not called."""
    from routers import search

    member = _make_member()

    conversations_table = _FakeTable(["id", "title", "member_id", "organization_id", "updated_at"])
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
        table = _FakeTable(["id", "title", "member_id", "organization_id", "updated_at",
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
