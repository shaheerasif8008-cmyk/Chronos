"""Task 10 — permission-aware source retrieval + citations tests (TDD).

Pure-mock: no real DB, no real embeddings. Mocking style mirrors
test_source_indexing.py — monkeypatch module-level engine/reflect_table/embed and
fake async results.
"""
import pytest
from unittest.mock import AsyncMock


VEC = [0.01] * 1536


def _ctx(project_id="proj-1", member_id="member-1", org_id="default"):
    from core.models import RequesterContext
    return RequesterContext(org_id=org_id, member_id=member_id, project_id=project_id)


class _FakeCol:
    def __eq__(self, other): return True


class _FakeMembersTable:
    class c:
        project_id = _FakeCol()
        member_id = _FakeCol()
        organization_id = _FakeCol()

    def select(self):
        return _Clause("select")


class _Clause:
    def __init__(self, kind="other"): self.kind = kind
    def where(self, *a, **kw): return self


def _fake_reflect():
    async def _reflect(_name):
        return _FakeMembersTable()
    return _reflect


def _build_engine(results):
    """results: list returned per execute() call (by index)."""
    idx = [0]

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def all(self_inner):
                    return data if isinstance(data, list) else ([data] if data else [])
            return M()
        def first(self):
            if isinstance(self._data, list):
                return self._data[0] if self._data else None
            return self._data

    class _FakeConn:
        async def execute(self, stmt, params=None):
            i = idx[0]; idx[0] += 1
            return _FakeResult(results[i] if i < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    return _FakeEngine()


# ─── member with chunks gets scoped citations ──────────────────────────────────

@pytest.mark.asyncio
async def test_member_gets_scoped_ordered_citations(monkeypatch):
    from memory import source_retrieval as sr

    mem_row = {"id": "pm-1", "project_id": "proj-1", "member_id": "member-1"}
    chunk_rows = [
        {"source_id": "src-1", "chunk_index": 0, "content": "alpha content",
         "source_title": "Doc A", "distance": 0.1},
        {"source_id": "src-2", "chunk_index": 3, "content": "beta content",
         "source_title": "Doc B", "distance": 0.2},
    ]
    # execute order: membership check, then vector search.
    engine = _build_engine([mem_row, chunk_rows])

    monkeypatch.setattr(sr, "engine", engine)
    monkeypatch.setattr(sr, "reflect_table", _fake_reflect())
    monkeypatch.setattr(sr, "embed", AsyncMock(return_value=list(VEC)))
    monkeypatch.setattr(sr.audit, "log", AsyncMock())

    out = await sr.retrieve_source_chunks("find alpha", _ctx())

    assert len(out) == 2
    assert all(isinstance(c, sr.Citation) for c in out)
    assert [c.source_id for c in out] == ["src-1", "src-2"]
    assert all(c.snippet for c in out)  # every citation has a non-empty snippet

    payload = sr.citations_payload(out)
    assert [p["marker"] for p in payload] == ["S1", "S2"]
    assert all(p["snippet"] for p in payload)


# ─── non-member gets nothing + denied audit ─────────────────────────────────────

@pytest.mark.asyncio
async def test_non_member_denied(monkeypatch):
    from memory import source_retrieval as sr

    engine = _build_engine([None])  # membership check returns no row
    audit_log = AsyncMock()

    monkeypatch.setattr(sr, "engine", engine)
    monkeypatch.setattr(sr, "reflect_table", _fake_reflect())
    monkeypatch.setattr(sr, "embed", AsyncMock(return_value=list(VEC)))
    monkeypatch.setattr(sr.audit, "log", audit_log)

    out = await sr.retrieve_source_chunks("anything", _ctx())

    assert out == []
    events = [call.args[0] for call in audit_log.call_args_list]
    assert "source_retrieve_denied" in events


# ─── no project ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_project_returns_empty(monkeypatch):
    from memory import source_retrieval as sr

    # embed must never be called.
    monkeypatch.setattr(sr, "embed", AsyncMock(side_effect=AssertionError("should not embed")))

    out = await sr.retrieve_source_chunks("anything", _ctx(project_id=None))
    assert out == []


# ─── embedding error / wrong dimension → [] (honest) ────────────────────────────

@pytest.mark.asyncio
async def test_embedding_exception_returns_empty(monkeypatch):
    from memory import source_retrieval as sr

    mem_row = {"id": "pm-1", "project_id": "proj-1", "member_id": "member-1"}
    engine = _build_engine([mem_row])

    monkeypatch.setattr(sr, "engine", engine)
    monkeypatch.setattr(sr, "reflect_table", _fake_reflect())
    monkeypatch.setattr(sr, "embed", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(sr.audit, "log", AsyncMock())

    out = await sr.retrieve_source_chunks("q", _ctx())
    assert out == []


@pytest.mark.asyncio
async def test_wrong_dimension_returns_empty(monkeypatch):
    from memory import source_retrieval as sr

    mem_row = {"id": "pm-1", "project_id": "proj-1", "member_id": "member-1"}
    engine = _build_engine([mem_row])
    audit_log = AsyncMock()

    monkeypatch.setattr(sr, "engine", engine)
    monkeypatch.setattr(sr, "reflect_table", _fake_reflect())
    monkeypatch.setattr(sr, "embed", AsyncMock(return_value=[0.1] * 10))  # wrong dim
    monkeypatch.setattr(sr.audit, "log", audit_log)

    out = await sr.retrieve_source_chunks("q", _ctx())
    assert out == []
    events = [call.args[0] for call in audit_log.call_args_list]
    assert "source_retrieve_error" in events


# ─── build_knowledge_block ───────────────────────────────────────────────────────

def test_build_knowledge_block_empty():
    from memory.source_retrieval import build_knowledge_block
    assert build_knowledge_block([]) == ""


def test_build_knowledge_block_markers():
    from memory.source_retrieval import build_knowledge_block, Citation

    citations = [
        Citation(source_id="s1", source_title="Doc A", chunk_index=0, snippet="alpha"),
        Citation(source_id="s2", source_title="Doc B", chunk_index=1, snippet="beta"),
    ]
    block = build_knowledge_block(citations)
    assert "# Project Knowledge" in block
    assert "[S1] Doc A" in block
    assert "[S2] Doc B" in block
    assert "alpha" in block and "beta" in block


# ─── citations_payload always carries snippet ───────────────────────────────────

def test_citations_payload_carries_snippet():
    from memory.source_retrieval import citations_payload, Citation

    citations = [Citation(source_id="s1", source_title="Doc A", chunk_index=2, snippet="alpha")]
    payload = citations_payload(citations)
    assert payload == [{
        "marker": "S1",
        "source_id": "s1",
        "source_title": "Doc A",
        "chunk_index": 2,
        "snippet": "alpha",
    }]
    assert all(p["snippet"] for p in payload)
