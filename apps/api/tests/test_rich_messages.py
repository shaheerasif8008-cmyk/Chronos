"""Rich message model — persist and return metadata fields.

TDD: run this file first (it fails before migration + router changes), then passes.

Mocking style: monkeypatch at the module level following test_settings.py.
"""
import pytest


# ── Unit: _save_message signature accepts and forwards new fields ─────────────

@pytest.mark.asyncio
async def test_save_message_new_kwargs_forwarded(monkeypatch):
    """_save_message must accept keyword-only metadata fields without error
    and include them in the INSERT values.
    """
    from routers import chat

    insert_values: dict = {}

    class _FakeInsertClause:
        def values(self, **kwargs):
            insert_values.update(kwargs)
            return self

    class _FakeUpdateClause:
        def where(self, *a):
            return self
        def values(self, **kw):
            return self

    class _FakeConn:
        async def execute(self, stmt):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    class _FakeEngine:
        def begin(self):
            return _FakeConn()

    class _FakeTable:
        class c:
            id = None
        def __init__(self): pass

    def fake_insert(_tbl):
        return _FakeInsertClause()

    def fake_update(_tbl):
        return _FakeUpdateClause()

    async def fake_reflect(_name):
        return _FakeTable()

    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", fake_reflect)
    monkeypatch.setattr(chat, "insert", fake_insert)
    monkeypatch.setattr(chat, "update", fake_update)

    # Must not raise with all new keyword-only args
    await chat._save_message(
        "conv-123",
        "assistant",
        "Hello world",
        model="gpt-4o",
        mode="chat",
        citations=[{"url": "https://example.com"}],
        tool_traces=[{"tool": "search", "summary": "done", "status": "complete"}],
        memory_refs=[{"id": "mem-1", "content": "some fact"}],
        artifact_refs=[{"id": "art-1", "title": "Report"}],
        approval_state=None,
        runtime_status="complete",
        parent_message_id=None,
    )

    # After implementation: new fields appear in INSERT values
    assert insert_values.get("model") == "gpt-4o", f"model missing from insert: {insert_values}"
    assert insert_values.get("mode") == "chat"
    assert insert_values.get("runtime_status") == "complete"
    assert insert_values.get("citations") == [{"url": "https://example.com"}]
    assert insert_values.get("tool_traces") == [
        {"tool": "search", "summary": "done", "status": "complete"}
    ]
    assert insert_values.get("artifact_refs") == [{"id": "art-1", "title": "Report"}]


# ── DB round-trip: insert with metadata → SELECT returns it ───────────────────

@pytest.mark.asyncio
async def test_round_trip_metadata_persisted_and_returned():
    """Insert a message row with rich metadata and verify SELECT returns Python
    lists/dicts (asyncpg deserialises JSONB automatically).

    Requires DATABASE_URL pointing at chronos_p23 with migration 0017 applied.
    """
    import os
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url or "chronos_p23" not in db_url:
        pytest.skip("Requires chronos_p23 DATABASE_URL")

    from sqlalchemy import insert, select, MetaData, Table
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(db_url, future=True)

    async def reflect(name: str) -> Table:
        meta = MetaData()
        async with engine.begin() as conn:
            return await conn.run_sync(lambda c: Table(name, meta, autoload_with=c))

    orgs = await reflect("organizations")
    members = await reflect("members")
    conversations = await reflect("conversations")
    messages = await reflect("messages")

    org_id = "10000000-0000-0000-0000-000000000001"
    member_id = "10000000-0000-0000-0000-000000000002"

    async with engine.begin() as conn:
        if not (await conn.execute(select(orgs.c.id).where(orgs.c.id == org_id))).first():
            await conn.execute(
                insert(orgs).values(id=org_id, slug="test-rich", name="Test Rich", region="us")
            )
        if not (await conn.execute(select(members.c.id).where(members.c.id == member_id))).first():
            await conn.execute(
                insert(members).values(
                    id=member_id, organization_id=org_id,
                    email="rich-test@example.com", role="user",
                )
            )
        conv_id = str((await conn.execute(
            insert(conversations).values(
                organization_id=org_id, region="us",
                member_id=member_id, title="Rich Test",
            ).returning(conversations.c.id)
        )).scalar_one())

        msg_id = str((await conn.execute(
            insert(messages).values(
                organization_id=org_id,
                region="us",
                conversation_id=conv_id,
                role="assistant",
                content="Hello with metadata",
                token_count=3,
                model="gpt-4o",
                mode="chat",
                citations=[{"url": "https://example.com", "text": "Ref 1"}],
                tool_traces=[{"id": "t1", "tool": "search", "summary": "done", "status": "complete"}],
                memory_refs=[{"id": "m1", "content": "fact"}],
                artifact_refs=[{"id": "a1", "title": "Report", "kind": "markdown"}],
                runtime_status="complete",
            ).returning(messages.c.id)
        )).scalar_one())

    async with engine.begin() as conn:
        rows = (await conn.execute(
            select(messages).where(messages.c.conversation_id == conv_id)
        )).mappings().all()

    assert len(rows) == 1
    row = dict(rows[0])
    assert str(row["id"]) == msg_id
    assert row["model"] == "gpt-4o"
    assert row["mode"] == "chat"
    assert row["runtime_status"] == "complete"
    # JSONB comes back as Python lists/dicts
    assert isinstance(row["citations"], list), f"expected list, got {type(row['citations'])}"
    assert row["citations"][0]["url"] == "https://example.com"
    assert isinstance(row["tool_traces"], list)
    assert row["tool_traces"][0]["tool"] == "search"
    assert isinstance(row["memory_refs"], list)
    assert row["memory_refs"][0]["content"] == "fact"
    assert isinstance(row["artifact_refs"], list)
    assert row["artifact_refs"][0]["title"] == "Report"

    await engine.dispose()


# ── Unit: list_messages returns all columns including new ones ────────────────

@pytest.mark.asyncio
async def test_list_messages_returns_rich_fields(monkeypatch):
    """list_messages endpoint returns new metadata fields in response dicts."""
    from routers import chat
    from core.models import Member

    member = Member(
        id="member-1",
        organization_id="default",
        email="admin@example.com",
        role="admin",
    )

    rich_row = {
        "id": "msg-1",
        "conversation_id": "conv-1",
        "role": "assistant",
        "content": "hello",
        "model": "gpt-4o",
        "mode": "chat",
        "citations": [{"url": "https://example.com"}],
        "tool_traces": [{"tool": "search", "summary": "done"}],
        "memory_refs": [],
        "artifact_refs": [{"id": "a1", "title": "doc", "kind": "markdown"}],
        "runtime_status": "complete",
        "approval_state": None,
        "parent_message_id": None,
        "pinned": False,
        "token_count": 1,
        "created_at": None,
        "organization_id": "default",
        "region": "us",
        "artifact_ids": [],
    }

    class _FakeResult:
        def mappings(self):
            class M:
                def all(self_inner):
                    return [rich_row]
            return M()

    class _FakeConn:
        async def execute(self, stmt):
            return _FakeResult()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    class _FakeEngine:
        def begin(self):
            return _FakeConn()

    class _FakeColumn:
        def __eq__(self, other):
            return True
        def asc(self):
            return self

    class _FakeTable:
        class c:
            conversation_id = _FakeColumn()
            created_at = _FakeColumn()

    class _FakeSelect:
        def where(self, *a):
            return self
        def order_by(self, *a):
            return self

    def fake_select(_tbl):
        return _FakeSelect()

    async def fake_reflect(_name):
        return _FakeTable()

    async def fake_permission(*args, **kwargs):
        return True

    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", fake_reflect)
    monkeypatch.setattr(chat, "select", fake_select)
    monkeypatch.setattr(chat.permissions, "check", fake_permission)

    result = await chat.list_messages("conv-1", member)

    assert len(result) == 1
    row = result[0]
    assert row["model"] == "gpt-4o", "model field missing from list_messages response"
    assert row["mode"] == "chat"
    assert row["runtime_status"] == "complete"
    assert row["citations"] == [{"url": "https://example.com"}]
    assert row["tool_traces"] == [{"tool": "search", "summary": "done"}]
    assert row["artifact_refs"] == [{"id": "a1", "title": "doc", "kind": "markdown"}]
