"""Task 8b — project_sources tests (TDD).

Mocking style mirrors test_projects.py / test_doc_parsing.py: monkeypatch
module-level engine/reflect_table/select/insert and fake async results.
"""
import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_member(member_id="member-1", org_id="default"):
    from core.models import Member
    return Member(id=member_id, organization_id=org_id, email="test@example.com", role="user")


class _FakeCol:
    def __eq__(self, other): return True
    def desc(self): return self


class _FakeTable:
    class c:
        id = _FakeCol()
        organization_id = _FakeCol()
        project_id = _FakeCol()
        member_id = _FakeCol()
        created_at = _FakeCol()


def _fake_reflect():
    async def _reflect(_name):
        return _FakeTable()
    return _reflect


class _Clause:
    def where(self, *a, **kw): return self
    def values(self, **kw): return self
    def returning(self, *a): return self
    def order_by(self, *a): return self
    def join(self, *a, **kw): return self


def _noop_select(*a, **kw): return _Clause()
def _noop_insert(_tbl): return _Clause()


def _build_engine(results):
    call_idx = [0]

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
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    return _FakeEngine()


# ─── POST /attachments with project_id ────────────────────────────────────────

@pytest.mark.asyncio
async def test_upload_with_project_id_creates_source(monkeypatch):
    """Upload with a project_id (member of project) creates a project_sources row."""
    from routers import attachments
    from routers import projects
    from io import BytesIO
    from starlette.datastructures import UploadFile as StarletteUploadFile, Headers

    member = _make_member()
    mem_row = {"id": "pm-1", "project_id": "proj-1", "member_id": "member-1", "role": "owner"}
    source_id = "src-99"

    async def fake_save(raw, **kw):
        return "att-123"

    captured = {}

    def capturing_insert(_tbl):
        class _Cap:
            def values(self_inner, **kwargs):
                captured.update(kwargs)
                return _Clause()
            def returning(self_inner, *a): return self_inner
        return _Cap()

    # _require_member runs one SELECT (membership row); then the source INSERT.
    engine = _build_engine([mem_row, source_id])

    monkeypatch.setattr(attachments, "save_artifact", fake_save)
    monkeypatch.setattr(attachments, "engine", engine)
    monkeypatch.setattr(attachments, "reflect_table", _fake_reflect())
    monkeypatch.setattr(attachments, "insert", capturing_insert)
    monkeypatch.setattr(attachments.audit, "log", AsyncMock())
    monkeypatch.setattr(attachments.permissions, "check", AsyncMock(return_value=True))
    # _require_member uses the projects module's engine/reflect/select.
    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)

    upload = StarletteUploadFile(
        filename="report.pdf",
        file=BytesIO(b"%PDF-1.4 data"),
        headers=Headers({"content-type": "application/pdf"}),
    )
    out = await attachments.upload_attachment(
        file=upload, conversation_id=None, project_id="proj-1", member=member
    )

    assert out["attachment_id"] == "att-123"
    assert out["source_id"] == source_id
    assert captured["source_type"] == "upload"
    assert captured["artifact_id"] == "att-123"
    assert captured["project_id"] == "proj-1"
    assert captured["organization_id"] == "default"


@pytest.mark.asyncio
async def test_upload_without_project_id_returns_null_source(monkeypatch):
    """Upload with no project_id stays backward compatible: source_id is None."""
    from routers import attachments
    from io import BytesIO
    from starlette.datastructures import UploadFile as StarletteUploadFile, Headers

    member = _make_member()

    async def fake_save(raw, **kw):
        return "att-123"

    insert_called = [0]

    def counting_insert(_tbl):
        insert_called[0] += 1
        return _Clause()

    monkeypatch.setattr(attachments, "save_artifact", fake_save)
    monkeypatch.setattr(attachments, "insert", counting_insert)
    monkeypatch.setattr(attachments.audit, "log", AsyncMock())
    monkeypatch.setattr(attachments.permissions, "check", AsyncMock(return_value=True))

    upload = StarletteUploadFile(
        filename="report.pdf",
        file=BytesIO(b"%PDF-1.4 data"),
        headers=Headers({"content-type": "application/pdf"}),
    )
    out = await attachments.upload_attachment(
        file=upload, conversation_id="c1", project_id=None, member=member
    )

    assert out["attachment_id"] == "att-123"
    assert out["source_id"] is None
    assert insert_called[0] == 0, "no project_sources insert when project_id absent"


@pytest.mark.asyncio
async def test_upload_with_project_id_non_member_raises_404(monkeypatch):
    """Upload with a project_id where caller is NOT a member raises 404."""
    from routers import attachments
    from routers import projects
    from io import BytesIO
    from starlette.datastructures import UploadFile as StarletteUploadFile, Headers

    member = _make_member(member_id="outsider")
    # _require_member finds no membership row → 404.
    engine = _build_engine([None])

    save_called = [0]

    async def fake_save(raw, **kw):
        save_called[0] += 1
        return "att-123"

    monkeypatch.setattr(attachments, "save_artifact", fake_save)
    monkeypatch.setattr(attachments.audit, "log", AsyncMock())
    monkeypatch.setattr(attachments.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)

    upload = StarletteUploadFile(
        filename="report.pdf",
        file=BytesIO(b"%PDF-1.4 data"),
        headers=Headers({"content-type": "application/pdf"}),
    )
    with pytest.raises(HTTPException) as exc_info:
        await attachments.upload_attachment(
            file=upload, conversation_id=None, project_id="proj-x", member=member
        )
    assert exc_info.value.status_code == 404
    assert save_called[0] == 0, "membership check must run before storing"


# ─── GET /projects/{id}/sources ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_project_sources_returns_org_scoped_rows(monkeypatch):
    """GET /projects/{id}/sources returns the project's sources for a member."""
    from routers import projects

    member = _make_member()
    mem_row = {"id": "pm-1", "project_id": "proj-1", "member_id": "member-1", "role": "owner"}
    source_rows = [
        {"id": "src-1", "project_id": "proj-1", "source_type": "upload", "organization_id": "default"},
        {"id": "src-2", "project_id": "proj-1", "source_type": "upload", "organization_id": "default"},
    ]

    engine = _build_engine([mem_row, source_rows])

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    result = await projects.get_project_sources("proj-1", member)
    assert len(result) == 2
    assert all(r["organization_id"] == "default" for r in result)


@pytest.mark.asyncio
async def test_get_project_sources_non_member_returns_404(monkeypatch):
    """Non-member GET of project sources returns 404."""
    from routers import projects

    member = _make_member(member_id="outsider")
    engine = _build_engine([None])

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc_info:
        await projects.get_project_sources("proj-1", member)
    assert exc_info.value.status_code == 404
