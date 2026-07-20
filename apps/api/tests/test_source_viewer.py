"""Task 12 — source viewer detail endpoint tests (TDD).

Mocking style mirrors test_source_indexing.py: monkeypatch module-level
engine/reflect_table/select with fake async results.
"""
import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock


def _make_member(member_id="member-1", org_id="default", role="user"):
    from core.models import Member
    return Member(id=member_id, organization_id=org_id, email="test@example.com", role=role)


class _FakeCol:
    def __eq__(self, other): return True
    def asc(self): return self
    def desc(self): return self


class _FakeTable:
    class c:
        id = _FakeCol()
        organization_id = _FakeCol()
        project_id = _FakeCol()
        source_id = _FakeCol()
        member_id = _FakeCol()
        chunk_index = _FakeCol()
        content = _FakeCol()
        token_count = _FakeCol()


def _fake_reflect():
    async def _reflect(_name):
        return _FakeTable()
    return _reflect


class _Clause:
    def where(self, *a, **kw): return self
    def order_by(self, *a): return self
    def limit(self, *a): return self
    def offset(self, *a): return self
    def join(self, *a, **kw): return self
    def select_from(self, *a, **kw): return self


def _noop_select(*a, **kw): return _Clause()


def _build_engine(results):
    idx = [0]

    class _FakeResult:
        def __init__(self, data): self._data = data
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

    class _FakeConn:
        async def execute(self, stmt, params=None):
            i = idx[0]; idx[0] += 1
            return _FakeResult(results[i] if i < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    return _FakeEngine()


@pytest.mark.asyncio
async def test_source_detail_returns_metadata_and_chunk_preview(monkeypatch):
    from routers import projects

    member = _make_member()
    source_row = {
        "id": "src-1", "project_id": "proj-1", "organization_id": "default",
        "title": "Report", "source_type": "upload", "uri": None,
        "artifact_id": "att-1", "parse_status": "parsed", "index_status": "indexed",
    }
    chunk_rows = [
        {"chunk_index": 0, "content": "first chunk", "token_count": 3},
        {"chunk_index": 1, "content": "second chunk", "token_count": 3},
    ]
    engine = _build_engine([source_row, 2, chunk_rows])

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(
        projects,
        "project_access_role",
        AsyncMock(return_value=({"id": "proj-1"}, "owner")),
    )
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    out = await projects.get_source_detail("proj-1", "src-1", member)
    assert out["id"] == "src-1"
    assert out["has_original"] is True
    assert out["download_url"] == "/projects/proj-1/sources/src-1/content"
    assert "artifact_id" not in out
    assert out["parse_status"] == "parsed"
    assert out["index_status"] == "indexed"
    assert out["warning"] is None
    assert out["chunk_count"] == 2
    assert len(out["chunks"]) == 2
    assert out["chunks"][0]["chunk_index"] == 0


@pytest.mark.asyncio
async def test_source_detail_surfaces_parse_warning(monkeypatch):
    from routers import projects

    member = _make_member()
    source_row = {
        "id": "src-1", "project_id": "proj-1", "organization_id": "default",
        "title": "Scan", "source_type": "upload", "uri": None,
        "artifact_id": "att-1", "parse_status": "unparseable", "index_status": "failed",
    }
    engine = _build_engine([source_row, 0, []])

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(
        projects,
        "project_access_role",
        AsyncMock(return_value=({"id": "proj-1"}, "owner")),
    )
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    out = await projects.get_source_detail("proj-1", "src-1", member)
    assert out["warning"] is not None
    assert "parse" in out["warning"].lower()
    assert out["chunk_count"] == 0
    assert out["chunks"] == []


@pytest.mark.asyncio
async def test_source_download_uses_source_acl_without_exposing_artifact_id(monkeypatch):
    from routers import projects

    member = _make_member()
    source = {
        "id": "src-1",
        "project_id": "proj-1",
        "organization_id": "default",
        "title": "Customer report.txt",
        "artifact_id": "private-artifact-id",
        "permissions": {},
        "created_by": "owner-1",
    }
    engine = _build_engine([source])
    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(
        projects,
        "project_access_role",
        AsyncMock(return_value=({"id": "proj-1"}, "member")),
    )
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(projects, "get_artifact", AsyncMock(return_value={"organization_id": "default", "mime_type": "text/plain"}))
    monkeypatch.setattr(projects, "read_artifact_content", AsyncMock(return_value=b"authorized source bytes"))

    response = await projects.download_source_content("proj-1", "src-1", member)
    assert response.body == b"authorized source bytes"
    assert response.headers["content-disposition"].startswith("attachment;")


@pytest.mark.asyncio
async def test_private_source_download_denies_other_project_member(monkeypatch):
    from routers import projects

    member = _make_member(member_id="viewer-1")
    private_source = {
        "id": "src-private",
        "project_id": "proj-1",
        "organization_id": "default",
        "artifact_id": "private-artifact-id",
        "permissions": {"visibility": "private"},
        "created_by": "owner-1",
    }
    engine = _build_engine([private_source])
    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(
        projects,
        "project_access_role",
        AsyncMock(return_value=({"id": "proj-1"}, "member")),
    )
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc:
        await projects.download_source_content("proj-1", "src-private", member)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_source_detail_non_member_404(monkeypatch):
    from routers import projects

    member = _make_member(member_id="outsider")
    monkeypatch.setattr(
        projects,
        "project_access_role",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc:
        await projects.get_source_detail("proj-1", "src-1", member)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_source_detail_404_when_source_not_in_project(monkeypatch):
    from routers import projects

    member = _make_member()
    engine = _build_engine([None])

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(
        projects,
        "project_access_role",
        AsyncMock(return_value=({"id": "proj-1"}, "owner")),
    )
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc:
        await projects.get_source_detail("proj-1", "missing", member)
    assert exc.value.status_code == 404
