"""Task 9 — source indexing tests (TDD).

Pure-mock: no real DB, no real embeddings. Mocking style mirrors
test_project_sources.py / test_doc_parsing.py — monkeypatch module-level
engine/reflect_table/select/insert/delete/update and fake async results; embed
returns a fixed 1536-dim vector.
"""
import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock


VEC = [0.01] * 1536


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_member(member_id="member-1", org_id="default", role="user"):
    from core.models import Member
    return Member(id=member_id, organization_id=org_id, email="test@example.com", role=role)


class _FakeCol:
    def __eq__(self, other): return True
    def desc(self): return self


class _FakeTable:
    class c:
        id = _FakeCol()
        organization_id = _FakeCol()
        project_id = _FakeCol()
        source_id = _FakeCol()
        member_id = _FakeCol()
        parent_artifact_id = _FakeCol()
        kind = _FakeCol()
        created_at = _FakeCol()


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
    def join(self, *a, **kw): return self
    def with_for_update(self, *a, **kw): return self


def _noop_select(*a, **kw): return _Clause("select")
def _noop_insert(_tbl): return _Clause("insert")
def _noop_update(_tbl): return _Clause("update")
def _noop_delete(_tbl): return _Clause("delete")


def _build_engine(results, ops):
    """results: list returned per execute() (by index). ops: list appended with kind."""
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
            kind = _kind(stmt)
            # executemany: insert(chunks), [row, row, ...] — record one "insert"
            # per row so ops.count("insert") still equals the chunk count.
            if kind == "insert" and isinstance(params, list):
                for _ in params:
                    ops.append("insert")
            else:
                ops.append(kind)
            i = idx[0]; idx[0] += 1
            return _FakeResult(results[i] if i < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    return _FakeEngine()


# ─── index_source: multi-chunk ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_index_source_produces_chunks_and_marks_indexed(monkeypatch):
    from memory import source_indexing as si

    source_row = {"id": "src-1", "project_id": "proj-1", "organization_id": "default",
                  "artifact_id": "att-1"}
    # ~7000 chars → multiple chunks (CHUNK_CHARS=3200, step=2800).
    long_text = "x" * 7000

    ops: list[str] = []
    # execute sequence: select(source), delete(idempotency), then 1 insert per chunk,
    # then update(index_status). _set_index_status uses its own begin() too.
    engine = _build_engine([source_row, 0, None, None, None, None, None], ops)

    async def fake_resolve(source, org_id):
        return long_text, False

    monkeypatch.setattr(si, "engine", engine)
    monkeypatch.setattr(si, "reflect_table", _fake_reflect())
    monkeypatch.setattr(si, "select", _noop_select)
    monkeypatch.setattr(si, "insert", _noop_insert)
    monkeypatch.setattr(si, "update", _noop_update)
    monkeypatch.setattr(si, "delete", _noop_delete)
    monkeypatch.setattr(si, "embed", AsyncMock(return_value=list(VEC)))
    monkeypatch.setattr(si, "_resolve_source_text", fake_resolve)
    monkeypatch.setattr(si.audit, "log", AsyncMock())

    out = await si.index_source("src-1", "default")

    assert out["index_status"] == "indexed"
    assert out["chunk_count"] >= 2
    assert ops.count("insert") == out["chunk_count"]
    # An idempotency delete is issued before any insert.
    assert ops.index("delete") < ops.index("insert")


# ─── idempotency ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_index_source_deletes_before_insert(monkeypatch):
    from memory import source_indexing as si

    source_row = {"id": "src-1", "project_id": "proj-1", "organization_id": "default",
                  "artifact_id": "att-1"}
    ops: list[str] = []
    engine = _build_engine([source_row, 0, None, None], ops)

    monkeypatch.setattr(si, "engine", engine)
    monkeypatch.setattr(si, "reflect_table", _fake_reflect())
    monkeypatch.setattr(si, "select", _noop_select)
    monkeypatch.setattr(si, "insert", _noop_insert)
    monkeypatch.setattr(si, "update", _noop_update)
    monkeypatch.setattr(si, "delete", _noop_delete)
    monkeypatch.setattr(si, "embed", AsyncMock(return_value=list(VEC)))
    monkeypatch.setattr(si, "_resolve_source_text", AsyncMock(return_value=("short text", False)))
    monkeypatch.setattr(si.audit, "log", AsyncMock())

    await si.index_source("src-1", "default")

    assert "delete" in ops
    assert "insert" in ops
    assert ops.index("delete") < ops.index("insert"), "must clear old chunks before inserting"


# ─── delete_source_chunks ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_source_chunks_scoped_and_counts(monkeypatch):
    from memory import source_indexing as si

    ops: list[str] = []
    engine = _build_engine([3], ops)  # rowcount 3

    monkeypatch.setattr(si, "engine", engine)
    monkeypatch.setattr(si, "reflect_table", _fake_reflect())
    monkeypatch.setattr(si, "delete", _noop_delete)

    count = await si.delete_source_chunks("src-1", "default")
    assert count == 3
    assert ops == ["delete"]


# ─── empty / no-text source ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_index_source_empty_text_indexed_zero_chunks(monkeypatch):
    from memory import source_indexing as si

    source_row = {"id": "src-1", "project_id": "proj-1", "organization_id": "default",
                  "artifact_id": "att-1"}
    ops: list[str] = []
    engine = _build_engine([source_row, 0, None], ops)

    monkeypatch.setattr(si, "engine", engine)
    monkeypatch.setattr(si, "reflect_table", _fake_reflect())
    monkeypatch.setattr(si, "select", _noop_select)
    monkeypatch.setattr(si, "insert", _noop_insert)
    monkeypatch.setattr(si, "update", _noop_update)
    monkeypatch.setattr(si, "delete", _noop_delete)
    monkeypatch.setattr(si, "embed", AsyncMock(return_value=list(VEC)))
    monkeypatch.setattr(si, "_resolve_source_text", AsyncMock(return_value=("", False)))
    monkeypatch.setattr(si.audit, "log", AsyncMock())

    out = await si.index_source("src-1", "default")
    assert out == {"source_id": "src-1", "chunk_count": 0, "index_status": "indexed"}
    assert "insert" not in ops, "no chunk inserts for empty text"


@pytest.mark.asyncio
async def test_index_source_parse_failure_marks_failed(monkeypatch):
    from memory import source_indexing as si

    source_row = {"id": "src-1", "project_id": "proj-1", "organization_id": "default",
                  "artifact_id": "att-1"}
    ops: list[str] = []
    engine = _build_engine([source_row, 0, None], ops)

    monkeypatch.setattr(si, "engine", engine)
    monkeypatch.setattr(si, "reflect_table", _fake_reflect())
    monkeypatch.setattr(si, "select", _noop_select)
    monkeypatch.setattr(si, "insert", _noop_insert)
    monkeypatch.setattr(si, "update", _noop_update)
    monkeypatch.setattr(si, "delete", _noop_delete)
    monkeypatch.setattr(si, "embed", AsyncMock(return_value=list(VEC)))
    monkeypatch.setattr(si, "_resolve_source_text", AsyncMock(return_value=("", True)))
    monkeypatch.setattr(si.audit, "log", AsyncMock())

    out = await si.index_source("src-1", "default")
    assert out["index_status"] == "failed"
    assert out["chunk_count"] == 0
    assert "insert" not in ops


@pytest.mark.asyncio
async def test_index_source_missing_returns_not_found(monkeypatch):
    from memory import source_indexing as si

    ops: list[str] = []
    engine = _build_engine([None], ops)  # no source row

    monkeypatch.setattr(si, "engine", engine)
    monkeypatch.setattr(si, "reflect_table", _fake_reflect())
    monkeypatch.setattr(si, "select", _noop_select)

    out = await si.index_source("nope", "default")
    assert out["index_status"] == "not_found"


@pytest.mark.asyncio
async def test_index_source_wrong_embedding_dim_fails_no_fake(monkeypatch):
    from memory import source_indexing as si

    source_row = {"id": "src-1", "project_id": "proj-1", "organization_id": "default",
                  "artifact_id": "att-1"}
    ops: list[str] = []
    engine = _build_engine([source_row, 0, None, None], ops)

    monkeypatch.setattr(si, "engine", engine)
    monkeypatch.setattr(si, "reflect_table", _fake_reflect())
    monkeypatch.setattr(si, "select", _noop_select)
    monkeypatch.setattr(si, "insert", _noop_insert)
    monkeypatch.setattr(si, "update", _noop_update)
    monkeypatch.setattr(si, "delete", _noop_delete)
    monkeypatch.setattr(si, "embed", AsyncMock(return_value=[0.1] * 10))  # wrong dim
    monkeypatch.setattr(si, "_resolve_source_text", AsyncMock(return_value=("some text", False)))
    monkeypatch.setattr(si.audit, "log", AsyncMock())

    out = await si.index_source("src-1", "default")
    assert out["index_status"] == "failed"
    assert out["chunk_count"] == 0


# ─── endpoint authz / 404 ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reindex_source_404_when_not_in_project(monkeypatch):
    from routers import projects

    member = _make_member()
    mem_row = {"id": "pm-1", "project_id": "proj-1", "member_id": "member-1", "role": "owner"}
    # _require_member → membership row; _require_source → None (not found).
    ops: list[str] = []
    engine = _build_engine([mem_row, None], ops)

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc:
        await projects.reindex_source("proj-1", "src-x", member)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_source_requires_owner(monkeypatch):
    from routers import projects

    member = _make_member(role="user")
    # _require_member returns a non-owner membership → _require_owner raises 403.
    mem_row = {"id": "pm-1", "project_id": "proj-1", "member_id": "member-1", "role": "member"}
    ops: list[str] = []
    engine = _build_engine([mem_row], ops)

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc:
        await projects.delete_source("proj-1", "src-1", member)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_source_owner_deletes_chunks_and_row(monkeypatch):
    from routers import projects

    member = _make_member(role="user")
    mem_row = {"id": "pm-1", "project_id": "proj-1", "member_id": "member-1", "role": "owner"}
    source_row = {"id": "src-1", "project_id": "proj-1", "organization_id": "default"}
    # _require_owner→_require_member (1 select), _require_source (1 select),
    # then delete_source_chunks via si (1 delete), then row delete (1 delete).
    ops: list[str] = []
    engine = _build_engine([mem_row, source_row, 2, None], ops)

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(projects, "delete", _noop_delete)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(projects.audit, "log", AsyncMock())

    from memory import source_indexing as si
    monkeypatch.setattr(si, "engine", engine)
    monkeypatch.setattr(si, "reflect_table", _fake_reflect())
    monkeypatch.setattr(si, "delete", _noop_delete)

    out = await projects.delete_source("proj-1", "src-1", member)
    assert out["deleted"] is True
    assert out["source_id"] == "src-1"
