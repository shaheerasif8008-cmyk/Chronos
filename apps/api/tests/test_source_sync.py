"""Task 11 — connector-synced knowledge tests (TDD).

Pure-mock: no real DB, no live OAuth. The "fixture connector" is a mocked
tool_broker.execute returning canned documents — this proves the sync path goes
THROUGH the broker seam (asserted) and never touches a connector directly. Mocking
style mirrors test_source_indexing.py.
"""
import pytest
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
                    if isinstance(data, list): return data
                    return [data] if data is not None else []
                def first(self_inner):
                    if isinstance(data, list): return data[0] if data else None
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
            i = idx[0]; idx[0] += 1
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
    monkeypatch.setattr(core.artifacts, "save_artifact", AsyncMock(return_value="art-1"))


def _feed(tool="gmail.search", args=None):
    return {
        "id": "feed-1",
        "organization_id": "default",
        "project_id": "proj-1",
        "connector_id": "conn-1",
        "source_type": "connector",
        "permissions": {"tool": tool, "args": args or {"query": "invoices"}},
    }


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
