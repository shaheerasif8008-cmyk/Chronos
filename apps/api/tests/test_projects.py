"""Projects router — TDD tests written before implementation.

Mocking style mirrors test_chat_controls.py: monkeypatch at module level.
"""
import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_member(member_id="member-1", org_id="default"):
    from core.models import Member
    return Member(id=member_id, organization_id=org_id, email="test@example.com", role="user")


class _FakeCol:
    def __init__(self, val=None):
        self._val = val
    def __eq__(self, other): return True
    def __le__(self, other): return True
    def __lt__(self, other): return True
    def asc(self): return self
    def desc(self): return self
    def is_(self, v): return True
    def in_(self, v): return True
    def ilike(self, v): return True
    def label(self, _name): return self


class _FakeTable:
    class c:
        id = _FakeCol()
        organization_id = _FakeCol()
        project_id = _FakeCol()
        member_id = _FakeCol()
        role = _FakeCol()
        name = _FakeCol()
        instructions = _FakeCol()
        visibility = _FakeCol()
        memory_policy = _FakeCol()
        default_tools = _FakeCol()
        conversation_id = _FakeCol()
        task_id = _FakeCol()
        created_at = _FakeCol()
        updated_at = _FakeCol()
        created_by = _FakeCol()


def _fake_reflect():
    async def _reflect(_name):
        return _FakeTable()
    return _reflect


class _Clause:
    def where(self, *a, **kw): return self
    def values(self, **kw): return self
    def returning(self, *a): return self
    def order_by(self, *a): return self
    def limit(self, *a): return self
    def offset(self, *a): return self
    def join(self, *a, **kw): return self
    def outerjoin(self, *a, **kw): return self
    def distinct(self, *a, **kw): return self
    def with_for_update(self, *a, **kw): return self


def _noop_select(*a, **kw): return _Clause()
def _noop_insert(_tbl): return _Clause()
def _noop_update(_tbl): return _Clause()
def _noop_delete(_tbl): return _Clause()


def _build_engine(rows_by_call=None, scalar_val=None):
    """Build fake async SQLAlchemy engine with sequential call results."""
    call_idx = [0]
    rows = rows_by_call or []
    sv = [scalar_val]

    class _FakeResult:
        def __init__(self, data):
            self._data = data

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

        def first(self):
            if isinstance(self._data, list):
                return self._data[0] if self._data else None
            return self._data

        def scalar_one(self):
            return sv[0]

        def scalar_one_or_none(self):
            return sv[0]

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]
            call_idx[0] += 1
            if idx < len(rows):
                return _FakeResult(rows[idx])
            return _FakeResult(sv[0])

        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    return _FakeEngine()


def _mock_project_access(monkeypatch, projects, *, role="owner", project_id="proj-1"):
    project = None if role is None else {"id": project_id, "organization_id": "default"}
    monkeypatch.setattr(
        projects,
        "project_access_role",
        AsyncMock(return_value=(project, role)),
    )


# ─── Create project ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_project_inserts_and_returns_id(monkeypatch):
    """POST /projects creates project, inserts owner membership, returns project_id."""
    from routers import projects

    member = _make_member()
    proj_id = "proj-111"

    call_idx = [0]
    scalar_vals = [proj_id, "pm-1"]  # project insert, project_members insert

    class _FakeResult:
        def __init__(self, sv): self._sv = sv
        def scalar_one(self): return self._sv
        def mappings(self):
            class M:
                def all(self_inner): return []
                def first(self_inner): return None
            return M()

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            if idx < len(scalar_vals):
                return _FakeResult(scalar_vals[idx])
            return _FakeResult(None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    monkeypatch.setattr(projects, "engine", _FakeEngine())
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(projects, "insert", _noop_insert)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(projects.audit, "log", AsyncMock())

    class Req:
        name = "My Project"
        instructions = None
        visibility = "private"

    result = await projects.create_project(Req(), member)
    assert "project_id" in result
    projects.audit.log.assert_awaited()


# ─── List projects (member-only) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_projects_returns_only_member_projects(monkeypatch):
    """GET /projects returns only projects where caller is a member."""
    from routers import projects

    member = _make_member()
    proj_rows = [
        {"id": "proj-1", "name": "P1", "organization_id": "default"},
        {"id": "proj-2", "name": "P2", "organization_id": "default"},
    ]

    engine = _build_engine(rows_by_call=[proj_rows])

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(projects.audit, "log", AsyncMock())

    result = await projects.list_projects(member)
    assert isinstance(result, list)
    # Projects belong to member's org
    assert len(result) == 2


def test_project_visibility_and_tool_policy_are_bounded():
    from core.project_access import (
        normalize_default_tools,
        normalize_visibility,
        tool_is_allowed,
    )

    assert normalize_visibility("ORGANIZATION") == "organization"
    assert normalize_default_tools(["browser", "browser", "gmail.search"]) == [
        "browser",
        "gmail.search",
    ]
    assert tool_is_allowed(["browser"], "browser__search") is True
    assert tool_is_allowed(["browser"], "gmail__search") is False
    assert tool_is_allowed(["browser"], "start_task") is True
    with pytest.raises(ValueError):
        normalize_visibility("public")
    with pytest.raises(ValueError):
        normalize_default_tools(["*"])


@pytest.mark.asyncio
async def test_organization_viewer_cannot_mutate_project(monkeypatch):
    from routers import projects

    monkeypatch.setattr(
        projects,
        "_require_member",
        AsyncMock(return_value={"id": "proj-org", "role": "viewer"}),
    )

    with pytest.raises(HTTPException) as exc_info:
        await projects._require_editor(_make_member(member_id="org-viewer"), "proj-org")

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Project membership required"


@pytest.mark.asyncio
async def test_explicit_project_member_can_mutate(monkeypatch):
    from routers import projects

    membership = {"id": "proj-member", "role": "member"}
    monkeypatch.setattr(projects, "_require_member", AsyncMock(return_value=membership))

    assert await projects._require_editor(_make_member(), "proj-member") == membership


@pytest.mark.asyncio
async def test_project_edit_access_rejects_synthetic_viewer(monkeypatch):
    from core import project_access

    member = _make_member(member_id="org-viewer")
    monkeypatch.setattr(
        project_access,
        "project_access_role",
        AsyncMock(return_value=({"id": "proj-org"}, "viewer")),
    )
    assert await project_access.member_can_edit_project(member, "proj-org") is False

    project_access.project_access_role.return_value = ({"id": "proj-org"}, "member")
    assert await project_access.member_can_edit_project(member, "proj-org") is True


@pytest.mark.asyncio
async def test_organization_visible_project_is_readable_without_membership(monkeypatch):
    from routers import projects

    member = _make_member(member_id="org-viewer")
    project = {
        "id": "proj-org",
        "name": "Shared project",
        "organization_id": "default",
        "visibility": "organization",
        "role": None,
    }
    monkeypatch.setattr(projects, "engine", _build_engine(rows_by_call=[project]))
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    _mock_project_access(monkeypatch, projects, role="viewer", project_id="proj-org")
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    result = await projects.get_project("proj-org", member)
    assert result["id"] == "proj-org"


@pytest.mark.asyncio
async def test_broker_rechecks_project_tool_policy_before_execution(monkeypatch):
    from core.exceptions import SafetyLimitViolation
    from core.models import AgentContext
    from core.tool_broker import tool_broker

    monkeypatch.setattr(
        "core.project_access.project_tool_allowlist",
        AsyncMock(return_value=["browser"]),
    )
    agent = AgentContext(
        id="project-agent",
        org_id="default",
        member_id="member-1",
        project_id="project-1",
    )
    with pytest.raises(SafetyLimitViolation, match="project's default tool policy"):
        await tool_broker.execute(agent, "code.python", {"code": "print('no')"})


# ─── GET /projects/{id} — not a member → 404 ─────────────────────────────────

@pytest.mark.asyncio
async def test_get_project_non_member_returns_404(monkeypatch):
    """Non-member GET of a project returns 404 (don't leak existence)."""
    from routers import projects

    member = _make_member(member_id="outsider")
    # No membership row found → _require_member raises 404
    engine = _build_engine(rows_by_call=[None])

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    _mock_project_access(monkeypatch, projects, role=None, project_id="proj-999")
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc_info:
        await projects.get_project("proj-999", member)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_project_cross_org_returns_404(monkeypatch):
    """Cross-org GET returns 404 (org filter enforced)."""
    from routers import projects

    member = _make_member(org_id="org-A")
    # membership lookup returns None (org filter applied)
    engine = _build_engine(rows_by_call=[None])

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    _mock_project_access(monkeypatch, projects, role=None, project_id="proj-org-B")
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc_info:
        await projects.get_project("proj-org-B", member)
    assert exc_info.value.status_code == 404


# ─── PATCH — owner-only, audit event for instructions ────────────────────────

@pytest.mark.asyncio
async def test_patch_project_owner_succeeds(monkeypatch):
    """Owner patching name succeeds; no project_instructions_updated when instructions absent."""
    from routers import projects

    member = _make_member()
    proj_row = {"id": "proj-1", "name": "Old Name", "organization_id": "default"}

    call_idx = [0]
    results = [proj_row, None]  # project, UPDATE

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
                def all(self_inner): return [data] if data else []
            return M()
        def first(self): return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    monkeypatch.setattr(projects, "engine", _FakeEngine())
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(projects, "insert", _noop_insert)
    monkeypatch.setattr(projects, "update", _noop_update)
    _mock_project_access(monkeypatch, projects)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(projects, "artifact_access", AsyncMock(return_value=(True, "owner")))
    monkeypatch.setattr(projects.audit, "log", AsyncMock())

    class PatchReq:
        def model_dump(self, exclude_unset=False):
            return {"name": "New Name"}

    result = await projects.patch_project("proj-1", PatchReq(), member)
    assert result["updated"] is True

    # No project_instructions_updated when instructions key absent
    for call in projects.audit.log.call_args_list:
        assert call.args[0] != "project_instructions_updated", \
            "project_instructions_updated must NOT fire when instructions not in patch body"


@pytest.mark.asyncio
async def test_patch_project_emits_instructions_event_when_present(monkeypatch):
    """PATCH with instructions key fires project_instructions_updated audit event."""
    from routers import projects

    member = _make_member()
    proj_row = {"id": "proj-1", "name": "P1", "organization_id": "default"}

    call_idx = [0]
    results = [proj_row, None]

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
                def all(self_inner): return [data] if data else []
            return M()
        def first(self): return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    monkeypatch.setattr(projects, "engine", _FakeEngine())
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(projects, "insert", _noop_insert)
    monkeypatch.setattr(projects, "update", _noop_update)
    _mock_project_access(monkeypatch, projects)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(projects.audit, "log", AsyncMock())

    class PatchReq:
        def model_dump(self, exclude_unset=False):
            return {"instructions": "New instructions"}

    await projects.patch_project("proj-1", PatchReq(), member)

    event_types = [call.args[0] for call in projects.audit.log.call_args_list]
    assert "project_instructions_updated" in event_types, \
        "project_instructions_updated must fire when instructions key is in patch body"


@pytest.mark.asyncio
async def test_patch_project_non_owner_returns_403(monkeypatch):
    """PATCH by non-owner member returns 403."""
    from routers import projects

    member = _make_member()
    mem_row = {"id": "pm-1", "project_id": "proj-1", "member_id": "member-1", "role": "member"}

    engine = _build_engine(rows_by_call=[mem_row])

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    _mock_project_access(monkeypatch, projects, role="member")
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    class PatchReq:
        def model_dump(self, exclude_unset=False):
            return {"name": "Hack"}

    with pytest.raises(HTTPException) as exc_info:
        await projects.patch_project("proj-1", PatchReq(), member)
    assert exc_info.value.status_code == 403


# ─── DELETE ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_project_owner_succeeds(monkeypatch):
    """DELETE /projects/{id} by owner succeeds."""
    from routers import projects

    member = _make_member()

    call_idx = [0]
    results = [None, None]  # DELETE project_members, DELETE project

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
            return M()
        def first(self): return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    monkeypatch.setattr(projects, "engine", _FakeEngine())
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(projects, "delete", _noop_delete)
    _mock_project_access(monkeypatch, projects)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(projects.audit, "log", AsyncMock())

    result = await projects.delete_project("proj-1", member)
    assert result["deleted"] is True


@pytest.mark.asyncio
async def test_delete_project_non_owner_returns_403(monkeypatch):
    """DELETE by non-owner member returns 403."""
    from routers import projects

    member = _make_member()
    mem_row = {"id": "pm-1", "project_id": "proj-1", "member_id": "member-1", "role": "member"}

    engine = _build_engine(rows_by_call=[mem_row])

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    _mock_project_access(monkeypatch, projects, role="member")
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc_info:
        await projects.delete_project("proj-1", member)
    assert exc_info.value.status_code == 403


# ─── POST /projects/{id}/members ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_member_by_owner_succeeds(monkeypatch):
    """POST /projects/{id}/members by owner inserts membership row."""
    from routers import projects

    member = _make_member()
    target_member_row = {"id": "member-2", "organization_id": "default", "email": "m2@example.com", "role": "user"}
    new_pm_id = "pm-new-99"

    call_idx = [0]
    # Target member existence check, idempotency check, INSERT.
    results = [target_member_row, None]

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
            return M()
        def first(self): return self._data
        def scalar_one(self): return new_pm_id

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    monkeypatch.setattr(projects, "engine", _FakeEngine())
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(projects, "insert", _noop_insert)
    _mock_project_access(monkeypatch, projects)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(projects.audit, "log", AsyncMock())

    class AddReq:
        member_id = "member-2"
        role = "member"

    result = await projects.add_project_member("proj-1", AddReq(), member)
    assert "member_id" in result
    projects.audit.log.assert_awaited()


@pytest.mark.asyncio
async def test_add_member_non_owner_returns_403(monkeypatch):
    """Non-owner trying to add a member returns 403."""
    from routers import projects

    member = _make_member()
    mem_row = {"id": "pm-1", "project_id": "proj-1", "member_id": "member-1", "role": "member"}

    engine = _build_engine(rows_by_call=[mem_row])

    monkeypatch.setattr(projects, "engine", engine)
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    _mock_project_access(monkeypatch, projects, role="member")
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    class AddReq:
        member_id = "member-2"
        role = "member"

    with pytest.raises(HTTPException) as exc_info:
        await projects.add_project_member("proj-1", AddReq(), member)
    assert exc_info.value.status_code == 403


# ─── DELETE /projects/{id}/members/{mid} ─────────────────────────────────────

@pytest.mark.asyncio
async def test_remove_last_owner_raises_400(monkeypatch):
    """Cannot remove the last owner — should raise 400."""
    from routers import projects

    member = _make_member()
    caller_row = {"id": "pm-1", "project_id": "proj-1", "member_id": "member-1", "role": "owner"}

    call_idx = [0]
    results = [[caller_row]]

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner):
                    if isinstance(data, list): return data[0] if data else None
                    return data
                def all(self_inner):
                    if isinstance(data, list): return data
                    return [data] if data else []
            return M()
        def first(self): return self._data
        def scalar_one(self):
            if isinstance(self._data, list): return len(self._data)
            return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    monkeypatch.setattr(projects, "engine", _FakeEngine())
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    _mock_project_access(monkeypatch, projects)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc_info:
        await projects.remove_project_member("proj-1", "member-1", member)
    assert exc_info.value.status_code in (400, 403)


# ─── GET /projects/{id}/conversations ────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_project_conversations_scoped_to_project(monkeypatch):
    """GET /projects/{id}/conversations returns only project's conversations."""
    from routers import projects

    member = _make_member()
    conv_rows = [
        {"id": "conv-1", "project_id": "proj-1", "title": "First"},
        {"id": "conv-2", "project_id": "proj-1", "title": "Second"},
    ]

    call_idx = [0]
    results = [conv_rows]

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner):
                    if isinstance(data, list): return data[0] if data else None
                    return data
                def all(self_inner):
                    if isinstance(data, list): return data
                    return [data] if data else []
            return M()
        def first(self):
            if isinstance(self._data, list): return self._data[0] if self._data else None
            return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    monkeypatch.setattr(projects, "engine", _FakeEngine())
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    _mock_project_access(monkeypatch, projects)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    result = await projects.get_project_conversations("proj-1", member)
    assert len(result) == 2


# ─── GET /projects/{id}/artifacts ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_project_artifacts_returns_linked_artifacts(monkeypatch):
    """GET /projects/{id}/artifacts returns artifacts linked via conversations/tasks."""
    from routers import projects

    member = _make_member()
    conv_ids = [{"id": "conv-1"}]
    task_ids = [{"id": "task-1"}]
    artifact_rows = [
        {"id": "art-1", "title": "A", "kind": "markdown"},
        {"id": "art-2", "title": "B", "kind": "file"},
    ]

    call_idx = [0]
    results = [conv_ids, task_ids, artifact_rows]

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def all(self_inner):
                    if isinstance(data, list): return data
                    return [data] if data else []
                def first(self_inner):
                    if isinstance(data, list): return data[0] if data else None
                    return data
            return M()
        def first(self):
            if isinstance(self._data, list): return self._data[0] if self._data else None
            return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    monkeypatch.setattr(projects, "engine", _FakeEngine())
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    _mock_project_access(monkeypatch, projects)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(projects, "artifact_access", AsyncMock(return_value=(True, "owner")))

    result = await projects.get_project_artifacts("proj-1", member)
    assert len(result) == 2


# ─── GET /projects/{id}/tasks ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_project_tasks_returns_project_tasks(monkeypatch):
    """GET /projects/{id}/tasks returns tasks with tasks.project_id == project."""
    from routers import projects

    member = _make_member()
    task_rows = [
        {"id": "task-1", "project_id": "proj-1", "goal": "Do something", "status": "complete"},
    ]

    call_idx = [0]
    results = [task_rows]

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def all(self_inner):
                    if isinstance(data, list): return data
                    return [data] if data else []
                def first(self_inner):
                    if isinstance(data, list): return data[0] if data else None
                    return data
            return M()
        def first(self):
            if isinstance(self._data, list): return self._data[0] if self._data else None
            return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    monkeypatch.setattr(projects, "engine", _FakeEngine())
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    _mock_project_access(monkeypatch, projects)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))

    result = await projects.get_project_tasks("proj-1", member)
    assert len(result) == 1
    assert result[0]["goal"] == "Do something"


# ─── Chat send with project_id ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_create_conversation_persists_project_id(monkeypatch):
    """_create_conversation persists project_id when provided."""
    from routers import chat

    member = _make_member()
    new_conv_id = "conv-proj-1"

    class _FakeResult:
        def scalar_one(self): return new_conv_id
        def mappings(self):
            class M:
                def all(self_inner): return []
            return M()

    class _FakeConn:
        async def execute(self, stmt): return _FakeResult()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    async def _reflect(_name):
        return _FakeTable()

    # Capture INSERT values
    inserted_values = {}
    def capturing_insert(tbl):
        class _CapturingClause:
            def values(self_inner, **kwargs):
                inserted_values.update(kwargs)
                return _Clause()
            def where(self_inner, *a, **kw): return self_inner
            def returning(self_inner, *a): return self_inner

        return _CapturingClause()

    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "insert", capturing_insert)
    monkeypatch.setattr(
        chat.workspace_access,
        "require_workspace_access",
        AsyncMock(return_value={"id": "workspace-abc"}),
    )

    conv_id = await chat._create_conversation(
        member,
        "Hello",
        project_id="proj-abc",
        workspace_id="workspace-abc",
    )
    assert conv_id == new_conv_id
    assert inserted_values.get("project_id") == "proj-abc"
    assert inserted_values.get("workspace_id") == "workspace-abc"


# ─── create_task_record persists project_id ──────────────────────────────────

@pytest.mark.asyncio
async def test_create_task_record_persists_project_id(monkeypatch):
    """create_task_record writes project_id to tasks table when provided."""
    from routers import tasks as tasks_router

    member = _make_member()
    task_id = "task-proj-123"

    inserted_values = {}

    class _FakeResult:
        def scalar_one(self): return task_id

    class _FakeConn:
        async def execute(self, stmt): return _FakeResult()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    async def _reflect(_name): return _FakeTable()

    def capturing_insert(tbl):
        class _Cap:
            def values(self_inner, **kwargs):
                inserted_values.update(kwargs)
                return _Clause()
            def returning(self_inner, *a): return self_inner
            def where(self_inner, *a, **kw): return self_inner

        return _Cap()

    monkeypatch.setattr(tasks_router, "engine", _FakeEngine())
    monkeypatch.setattr(tasks_router, "reflect_table", _reflect)
    monkeypatch.setattr(tasks_router, "insert", capturing_insert)
    monkeypatch.setattr(tasks_router.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(tasks_router, "_require_project_membership", AsyncMock())

    # Patch resolve_agent_model to avoid LLM import issues
    import core.llm as llm_module
    monkeypatch.setattr(llm_module, "resolve_agent_model", lambda m: "test-model")

    result = await tasks_router.create_task_record(
        goal="test goal",
        member=member,
        triggered_by="conv-1",
        project_id="proj-xyz",
    )
    assert result == task_id
    assert inserted_values.get("project_id") == "proj-xyz"


# ─── Fix 1: TOCTOU owner-count lock ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_remove_owner_locks_owner_count(monkeypatch):
    """remove_project_member calls .with_for_update() on the owner SELECT.

    Concurrency is not testable against mocks; this test verifies the SELECT
    wrapper is called so that the locking statement is emitted in production.
    Note: a full concurrency stress test would require two concurrent DB
    transactions against a live Postgres instance and is deferred.
    """
    from routers import projects

    member = _make_member()
    caller_row = {"id": "pm-1", "project_id": "proj-1", "member_id": "member-1", "role": "owner"}
    # Two owners — removal is allowed
    other_owner_row = {"id": "pm-2", "project_id": "proj-1", "member_id": "member-X", "role": "owner"}

    with_for_update_called = [False]

    class _TrackingClause:
        def where(self, *a, **kw): return self
        def values(self, **kw): return self
        def returning(self, *a): return self
        def order_by(self, *a): return self
        def limit(self, *a): return self
        def offset(self, *a): return self
        def join(self, *a, **kw): return self
        def outerjoin(self, *a, **kw): return self
        def with_for_update(self, *a, **kw):
            with_for_update_called[0] = True
            return self

    call_idx = [0]
    results = [[caller_row, other_owner_row], None]  # owners, DELETE

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner):
                    if isinstance(data, list): return data[0] if data else None
                    return data
                def all(self_inner):
                    if isinstance(data, list): return data
                    return [data] if data else []
            return M()
        def first(self): return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    def _tracking_select(*a, **kw):
        return _TrackingClause()

    monkeypatch.setattr(projects, "engine", _FakeEngine())
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _tracking_select)
    monkeypatch.setattr(projects, "delete", _noop_delete)
    _mock_project_access(monkeypatch, projects)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(projects.audit, "log", AsyncMock())

    result = await projects.remove_project_member("proj-1", "member-X", member)
    assert result["removed"] is True
    assert with_for_update_called[0], ".with_for_update() must be called on the owner SELECT"


# ─── Fix 2: member existence validation ──────────────────────────────────────

@pytest.mark.asyncio
async def test_add_member_rejects_unknown_member_id(monkeypatch):
    """add_project_member returns 404 when target member_id does not exist in org."""
    from routers import projects

    member = _make_member()

    call_idx = [0]
    results = [None]

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
                def all(self_inner): return [data] if data else []
            return M()
        def first(self): return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    monkeypatch.setattr(projects, "engine", _FakeEngine())
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    _mock_project_access(monkeypatch, projects)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(projects.audit, "log", AsyncMock())

    class AddReq:
        member_id = "nonexistent-member"
        role = "member"

    with pytest.raises(HTTPException) as exc_info:
        await projects.add_project_member("proj-1", AddReq(), member)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_add_member_idempotent_on_duplicate(monkeypatch):
    """add_project_member is idempotent: calling twice results in a single membership row."""
    from routers import projects

    member = _make_member()
    target_member_row = {"id": "member-2", "organization_id": "default", "email": "m2@example.com", "role": "user"}
    existing_pm_row = {"id": "pm-2", "project_id": "proj-1", "member_id": "member-2", "role": "member"}

    insert_called = [0]

    def counting_insert(tbl):
        insert_called[0] += 1
        return _Clause()

    call_idx = [0]
    # Target existence and idempotency check (already exists) → no INSERT.
    results = [target_member_row, existing_pm_row]

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
                def all(self_inner): return [data] if data else []
            return M()
        def first(self): return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    monkeypatch.setattr(projects, "engine", _FakeEngine())
    monkeypatch.setattr(projects, "reflect_table", _fake_reflect())
    monkeypatch.setattr(projects, "select", _noop_select)
    monkeypatch.setattr(projects, "insert", counting_insert)
    _mock_project_access(monkeypatch, projects)
    monkeypatch.setattr(projects.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(projects.audit, "log", AsyncMock())

    class AddReq:
        member_id = "member-2"
        role = "member"

    result = await projects.add_project_member("proj-1", AddReq(), member)
    assert result["member_id"] == "member-2"
    assert insert_called[0] == 0, "INSERT must not be called when membership already exists"
