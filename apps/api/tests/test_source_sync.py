"""Task 11 — connector-synced knowledge tests (TDD).

Pure-mock: no real DB, no live OAuth. The "fixture connector" is a mocked
tool_broker.execute returning canned documents — this proves the sync path goes
THROUGH the broker seam (asserted) and never touches a connector directly. Mocking
style mirrors test_source_indexing.py.
"""
import pytest
import sqlalchemy as sa
import base64
from datetime import datetime, timezone
from unittest.mock import AsyncMock


# ─── Helpers ──────────────────────────────────────────────────────────────────

class _FakeCol:
    def __eq__(self, other): return True
    def desc(self): return self


class _FakeTable:
    class c:
        id = _FakeCol()
        organization_id = _FakeCol()
        project_id = _FakeCol()
        connector_id = _FakeCol()
        source_id = _FakeCol()
        uri = _FakeCol()
        source_type = _FakeCol()
        parent_source_id = _FakeCol()


def _fake_reflect():
    async def _reflect(_name):
        return _FakeTable()
    return _reflect


class _Clause:
    def __init__(self, kind="other"): self.kind = kind
    def where(self, *a, **kw): return self
    def values(self, **kw): return self
    def returning(self, *a): return self
    def order_by(self, *a): return self


def _noop_select(*a, **kw): return _Clause("select")
def _noop_insert(_tbl): return _Clause("insert")
def _noop_update(_tbl): return _Clause("update")


def _build_engine(results, ops):
    idx = [0]

    class _FakeResult:
        def __init__(self, data):
            self._data = data
            self.rowcount = data if isinstance(data, int) else 0
        def mappings(self):
            data = self._data
            class M:
                def all(self_inner):
                    if isinstance(data, list):
                        return data
                    return [data] if data is not None else []
                def first(self_inner):
                    if isinstance(data, list):
                        return data[0] if data else None
                    return data
            return M()
        def scalar_one(self): return self._data

    def _kind(stmt):
        if isinstance(stmt, _Clause):
            return stmt.kind
        name = type(stmt).__name__.lower()
        for k in ("delete", "insert", "update", "select"):
            if k in name:
                return k
        return "other"

    class _FakeConn:
        async def execute(self, stmt, params=None):
            ops.append(_kind(stmt))
            i = idx[0]
            idx[0] += 1
            return _FakeResult(results[i] if i < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    return _FakeEngine()


def _patch_module(monkeypatch, ss, engine, ops):
    import core.artifacts
    monkeypatch.setattr(ss, "engine", engine)
    monkeypatch.setattr(ss, "reflect_table", _fake_reflect())
    monkeypatch.setattr(ss, "select", _noop_select)
    monkeypatch.setattr(ss, "insert", _noop_insert)
    monkeypatch.setattr(ss, "update", _noop_update)
    monkeypatch.setattr(ss.audit, "log", AsyncMock())
    monkeypatch.setattr(ss, "_validate_feed_access", AsyncMock(return_value=(True, "authorized")))
    monkeypatch.setattr(ss, "_delete_stale_feed_documents", AsyncMock(return_value=0))
    monkeypatch.setattr(ss, "delete_source_chunks", AsyncMock(return_value=0))
    monkeypatch.setattr(core.artifacts, "save_artifact", AsyncMock(return_value="art-1"))


def _feed(tool="gmail.search", args=None):
    return {
        "id": "feed-1",
        "organization_id": "default",
        "project_id": "proj-1",
        "connector_id": "conn-1",
        "created_by": "member-1",
        "source_type": "connector",
        "permissions": {"tool": tool, "args": args or {"query": "invoices"}},
    }


@pytest.mark.asyncio
async def test_feed_access_revalidates_active_owner_membership_and_bound_connector(monkeypatch):
    from core import connector_tools
    from jobs import source_sync as ss

    tables = {
        "members": sa.table("members", sa.column("id"), sa.column("organization_id"), sa.column("status")),
        "project_members": sa.table("project_members", sa.column("member_id"), sa.column("organization_id"), sa.column("project_id")),
        "connectors": sa.table("connectors", sa.column("id"), sa.column("organization_id"), sa.column("status"), sa.column("member_id"), sa.column("provider")),
    }

    async def reflect(name): return tables[name]

    class Result:
        def __init__(self, value): self.value = value
        def first(self): return self.value
        def mappings(self): return self

    def engine_for(*values):
        queue = list(values)
        class Conn:
            async def execute(self, stmt): return Result(queue.pop(0))
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return None
        class Engine:
            def begin(self): return Conn()
        return Engine()

    monkeypatch.setattr(ss, "reflect_table", reflect)
    monkeypatch.setattr(connector_tools, "member_connector_clause", lambda *args: sa.true())
    feed = {**_feed(), "created_by": "member-1"}

    monkeypatch.setattr(ss, "engine", engine_for(("member-1",), {"provider": "gmail"}))
    assert await ss._validate_feed_access(feed, "default") == (True, "authorized")

    monkeypatch.setattr(ss, "engine", engine_for(None, {"provider": "gmail"}))
    allowed, reason = await ss._validate_feed_access(feed, "default")
    assert allowed is False and reason == "owner_inactive_or_project_access_revoked"

    monkeypatch.setattr(ss, "engine", engine_for(("member-1",), {"provider": "slack"}))
    allowed, reason = await ss._validate_feed_access(feed, "default")
    assert allowed is False and reason == "connector_tool_mismatch"


@pytest.mark.asyncio
async def test_feed_refresh_removes_only_documents_missing_from_that_feed(monkeypatch):
    from jobs import source_sync as ss

    table = sa.table(
        "project_sources",
        sa.column("id"), sa.column("uri"), sa.column("organization_id"), sa.column("parent_source_id"),
    )
    async def reflect(_name): return table

    class Result:
        def __init__(self, rows=None): self.rows = rows or []
        def mappings(self): return self
        def all(self): return self.rows
    calls = [Result([{"id": "keep", "uri": "doc-1"}, {"id": "stale", "uri": "doc-2"}]), Result()]
    class Conn:
        async def execute(self, stmt): return calls.pop(0)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
    class Engine:
        def begin(self): return Conn()

    deleted = AsyncMock(return_value=1)
    monkeypatch.setattr(ss, "engine", Engine())
    monkeypatch.setattr(ss, "reflect_table", reflect)
    monkeypatch.setattr(ss, "delete_source_chunks", deleted)

    count = await ss._delete_stale_feed_documents(
        {"id": "feed-1"}, "default", {"doc-1"}
    )
    assert count == 1
    deleted.assert_awaited_once_with("stale", "default")


# ─── sync_connector_source ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_fetches_through_broker_and_indexes_each_doc(monkeypatch):
    from jobs import source_sync as ss
    from core.models import ToolResult

    docs = [
        {"external_id": "d1", "title": "Doc 1", "content": "hello world one"},
        {"external_id": "d2", "title": "Doc 2", "content": "hello world two"},
    ]
    # _load(select feed); per doc: _upsert(select existing=None, insert→id); final update.
    ops: list[str] = []
    engine = _build_engine(
        [_feed(), None, "doc-1", None, "doc-2", None], ops
    )
    _patch_module(monkeypatch, ss, engine, ops)

    broker = AsyncMock(return_value=ToolResult(data={"documents": docs}, summary="ok"))
    monkeypatch.setattr(ss.tool_broker, "execute", broker)
    index = AsyncMock(return_value={"source_id": "doc", "chunk_count": 2, "index_status": "indexed"})
    monkeypatch.setattr(ss, "index_source", index)

    out = await ss.sync_connector_source("feed-1", "default")

    # Fetched THROUGH the broker with the feed's tool + args.
    assert broker.await_count == 1
    agent_arg, tool_arg, args_arg = broker.await_args.args
    assert tool_arg == "gmail.search"
    assert args_arg == {"query": "invoices"}
    assert agent_arg.org_id == "default"
    # Each document indexed via the Task 9 pipeline.
    assert index.await_count == 2
    assert out["synced"] == 2
    assert out["indexed"] == 2
    assert out["index_status"] == "synced"


@pytest.mark.asyncio
async def test_sync_broker_failure_marks_failed_no_fabrication(monkeypatch):
    from jobs import source_sync as ss

    ops: list[str] = []
    # _load(select feed); then _set_index_status(update).
    engine = _build_engine([_feed(), None], ops)
    _patch_module(monkeypatch, ss, engine, ops)

    monkeypatch.setattr(ss.tool_broker, "execute", AsyncMock(side_effect=RuntimeError("boom")))
    index = AsyncMock()
    monkeypatch.setattr(ss, "index_source", index)

    out = await ss.sync_connector_source("feed-1", "default")

    assert out["index_status"] == "failed"
    assert out["synced"] == 0
    assert index.await_count == 0, "no documents indexed when the broker fails"


@pytest.mark.asyncio
async def test_sync_missing_tool_spec_marks_failed(monkeypatch):
    from jobs import source_sync as ss

    ops: list[str] = []
    feed = _feed()
    feed["permissions"] = {}  # no tool
    engine = _build_engine([feed, None], ops)
    _patch_module(monkeypatch, ss, engine, ops)

    broker = AsyncMock()
    monkeypatch.setattr(ss.tool_broker, "execute", broker)

    out = await ss.sync_connector_source("feed-1", "default")

    assert out["index_status"] == "failed"
    assert broker.await_count == 0, "must not call the broker without a fetch spec"


@pytest.mark.asyncio
async def test_sync_not_a_connector_source_returns_not_found(monkeypatch):
    from jobs import source_sync as ss

    ops: list[str] = []
    upload = _feed()
    upload["source_type"] = "upload"
    engine = _build_engine([upload], ops)
    _patch_module(monkeypatch, ss, engine, ops)
    monkeypatch.setattr(ss.tool_broker, "execute", AsyncMock())

    out = await ss.sync_connector_source("feed-1", "default")
    assert out["index_status"] == "not_found"


@pytest.mark.asyncio
async def test_sync_partial_index_failure_reported_honestly(monkeypatch):
    from jobs import source_sync as ss
    from core.models import ToolResult

    docs = [
        {"external_id": "d1", "title": "Doc 1", "content": "a"},
        {"external_id": "d2", "title": "Doc 2", "content": "b"},
    ]
    ops: list[str] = []
    engine = _build_engine([_feed(), None, "doc-1", None, "doc-2", None], ops)
    _patch_module(monkeypatch, ss, engine, ops)
    monkeypatch.setattr(
        ss.tool_broker, "execute",
        AsyncMock(return_value=ToolResult(data={"documents": docs}, summary="ok")),
    )
    # First doc indexes, second fails.
    index = AsyncMock(side_effect=[
        {"index_status": "indexed", "chunk_count": 1},
        {"index_status": "failed", "chunk_count": 0},
    ])
    monkeypatch.setattr(ss, "index_source", index)

    out = await ss.sync_connector_source("feed-1", "default")
    assert out["indexed"] == 1
    assert out["index_status"] == "failed", "any failed doc surfaces as a failed sync"


# ─── revoke_connector_sources ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_revoke_removes_chunks_and_marks_revoked(monkeypatch):
    from jobs import source_sync as ss

    ops: list[str] = []
    # select(ids) → 2 rows; then one update marking them revoked.
    engine = _build_engine([[{"id": "s1"}, {"id": "s2"}], None], ops)
    _patch_module(monkeypatch, ss, engine, ops)

    dsc = AsyncMock(return_value=3)
    monkeypatch.setattr(ss, "delete_source_chunks", dsc)

    count = await ss.revoke_connector_sources("conn-1", "default")

    assert count == 2
    assert dsc.await_count == 2, "chunks deleted for every connector source"
    assert "update" in ops, "rows marked revoked"


@pytest.mark.asyncio
async def test_revoke_no_sources_is_a_noop(monkeypatch):
    from jobs import source_sync as ss

    ops: list[str] = []
    engine = _build_engine([[]], ops)  # no rows
    _patch_module(monkeypatch, ss, engine, ops)
    dsc = AsyncMock()
    monkeypatch.setattr(ss, "delete_source_chunks", dsc)

    count = await ss.revoke_connector_sources("conn-x", "default")
    assert count == 0
    assert dsc.await_count == 0
    assert "update" not in ops


# ─── normalize_documents — connector-shape breadth ───────────────────────────────

def test_normalize_passthrough_documents_shape():
    from jobs.source_sync import normalize_documents

    docs = normalize_documents({"documents": [{"external_id": "d1", "title": "T", "content": "body"}]})
    assert docs == [{"external_id": "d1", "title": "T", "content": "body", "permissions": {}}]


def test_normalize_gmail_threads_shape():
    from jobs.source_sync import normalize_documents

    # gmail.search returns {"threads": [{"id", "snippet", ...}]} — no "documents".
    docs = normalize_documents({"threads": [{"id": "t1", "snippet": "hi there team"}]})
    assert docs[0]["external_id"] == "t1"
    assert docs[0]["content"] == "hi there team"
    assert docs[0]["title"] == "hi there team"


def test_normalize_graph_value_and_bare_list():
    from jobs.source_sync import normalize_documents

    graph = normalize_documents({"value": [{"id": "m1", "subject": "Re: invoice", "body": "see attached"}]})
    assert graph[0]["external_id"] == "m1"
    assert graph[0]["title"] == "Re: invoice"
    assert graph[0]["content"] == "see attached"

    bare = normalize_documents([{"id": "x", "text": "plain"}])
    assert bare[0]["content"] == "plain"


def test_normalize_content_fallback_to_json_record():
    from jobs.source_sync import normalize_documents

    # No text-like field → keep the record verbatim rather than fabricating.
    docs = normalize_documents({"rows": [{"id": "r1", "amount": 42, "vendor": "ACME"}]})
    assert docs[0]["external_id"] == "r1"
    assert "ACME" in docs[0]["content"] and "42" in docs[0]["content"]


@pytest.mark.asyncio
async def test_sync_indexes_real_gmail_search_threads(monkeypatch):
    """A real connector (gmail.search) returning threads syncs end-to-end."""
    from jobs import source_sync as ss
    from core.models import ToolResult

    threads = [
        {"id": "t1", "snippet": "Invoice from ACME due Friday"},
        {"id": "t2", "snippet": "Receipt for your order"},
    ]
    ops: list[str] = []
    engine = _build_engine([_feed(tool="gmail.search"), None, "doc-1", None, "doc-2", None], ops)
    _patch_module(monkeypatch, ss, engine, ops)

    broker = AsyncMock(return_value=ToolResult(data={"threads": threads}, summary="2 results"))
    monkeypatch.setattr(ss.tool_broker, "execute", broker)
    index = AsyncMock(return_value={"index_status": "indexed", "chunk_count": 1})
    monkeypatch.setattr(ss, "index_source", index)

    out = await ss.sync_connector_source("feed-1", "default")

    # Fetched gmail.search through the broker; both threads indexed.
    assert broker.await_args.args[1] == "gmail.search"
    assert index.await_count == 2
    assert out["synced"] == 2
    assert out["indexed"] == 2
    assert out["index_status"] == "synced"


@pytest.mark.asyncio
async def test_connector_binary_is_scanned_and_only_sanitized_text_is_persisted(monkeypatch):
    from core.content_disarm import ContentDisarmResult
    from core.file_security import FileScanResult
    from core.models import ToolResult
    from jobs import source_sync as ss
    import core.artifacts

    original = b"%PDF-1.7 provider binary"
    docs = [{
        "id": "binary-1",
        "name": "brief.pdf",
        "mime_type": "application/pdf",
        "content_b64": base64.b64encode(original).decode(),
    }]
    ops: list[str] = []
    engine = _build_engine([_feed(), None, "doc-1", None], ops)
    _patch_module(monkeypatch, ss, engine, ops)
    monkeypatch.setattr(ss.tool_broker, "execute", AsyncMock(return_value=ToolResult(data={"documents": docs}, summary="ok")))
    scan = FileScanResult(
        verdict="clean", sha256="a" * 64, size_bytes=len(original),
        engine="clamav", scanned_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(ss, "scan_file_bytes", AsyncMock(return_value=scan))
    monkeypatch.setattr(ss, "disarm_connector_binary", AsyncMock(return_value=ContentDisarmResult("sanitized", None, b"Safe brief text", "text/plain")))
    event = AsyncMock(return_value="event-1")
    monkeypatch.setattr(ss, "record_file_security_event_if_available", event)
    index = AsyncMock(return_value={"index_status": "indexed", "chunk_count": 1})
    monkeypatch.setattr(ss, "index_source", index)

    out = await ss.sync_connector_source("feed-1", "default")

    assert out["index_status"] == "synced"
    assert core.artifacts.save_artifact.await_args.args[0] == b"Safe brief text"
    assert original not in core.artifacts.save_artifact.await_args.args
    assert core.artifacts.save_artifact.await_args.kwargs["mime_type"] == "text/plain"
    assert event.await_args.kwargs["content_disarm_status"] == "sanitized"


@pytest.mark.asyncio
async def test_infected_connector_replacement_is_quarantined_without_persistence(monkeypatch):
    from core.file_security import FileScanResult
    from core.models import ToolResult
    from jobs import source_sync as ss
    import core.artifacts

    original = b"infected-provider-binary"
    docs = [{"id": "binary-1", "name": "payload.pdf", "content_b64": base64.b64encode(original).decode()}]
    ops: list[str] = []
    engine = _build_engine([_feed(), None], ops)
    _patch_module(monkeypatch, ss, engine, ops)
    monkeypatch.setattr(ss.tool_broker, "execute", AsyncMock(return_value=ToolResult(data={"documents": docs}, summary="ok")))
    monkeypatch.setattr(ss, "scan_file_bytes", AsyncMock(return_value=FileScanResult(
        verdict="infected", sha256="b" * 64, size_bytes=len(original), signature="Eicar-Test-Signature",
        scanned_at=datetime.now(timezone.utc),
    )))
    quarantine = AsyncMock()
    monkeypatch.setattr(ss, "_quarantine_feed_document", quarantine)
    event = AsyncMock(return_value="event-1")
    monkeypatch.setattr(ss, "record_file_security_event_if_available", event)
    index = AsyncMock()
    monkeypatch.setattr(ss, "index_source", index)

    out = await ss.sync_connector_source("feed-1", "default")

    assert out["quarantined"] == 1
    assert out["index_status"] == "failed"
    quarantine.assert_awaited_once_with(_feed(), "default", "binary-1")
    core.artifacts.save_artifact.assert_not_awaited()
    index.assert_not_awaited()
