"""Chat controls — per-message actions (pin, edit, branch, save-memory, convert-task).

TDD: tests are written first and fail until the endpoints are implemented.
Mocking style mirrors test_rich_messages.py: monkeypatch at module level.
"""
import pytest
from fastapi import HTTPException


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_member(member_id="member-1", org_id="default"):
    from core.models import Member
    return Member(id=member_id, organization_id=org_id, email="test@example.com", role="user")


def _fake_engine_factory(rows_by_call=None, returning_scalar=None):
    """Build a fake async SQLAlchemy engine.

    rows_by_call: list of 'rows' to return on successive conn.execute() calls
                  (each item is None | a single value | a list).
    returning_scalar: if provided, scalar_one() returns this on the last execute.
    """
    call_idx = [0]
    rows = rows_by_call or []
    scalar_val = [returning_scalar]

    class _FakeResult:
        def __init__(self, row_data):
            self._data = row_data

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

        def first(self):
            if isinstance(self._data, list):
                return self._data[0] if self._data else None
            return self._data

        def scalar_one(self):
            return scalar_val[0]

        def scalar_one_or_none(self):
            return scalar_val[0]

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]
            call_idx[0] += 1
            if idx < len(rows):
                return _FakeResult(rows[idx])
            return _FakeResult(scalar_val[0])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class _FakeEngine:
        def begin(self):
            return _FakeConn()

    return _FakeEngine()


def _fake_sql_builder():
    """Returns a simple no-op SQL clause builder."""
    class _Clause:
        def where(self, *a, **kw):
            return self
        def values(self, **kw):
            return self
        def returning(self, *a):
            return self
        def order_by(self, *a):
            return self
        def limit(self, *a):
            return self
        def offset(self, *a):
            return self

    def _select(*a, **kw):
        return _Clause()

    def _insert(_tbl):
        return _Clause()

    def _update(_tbl):
        return _Clause()

    async def _reflect(_name):
        class _FakeCol:
            def __eq__(self, other): return True
            def __le__(self, other): return True
            def __lt__(self, other): return True
            def asc(self): return self
            def desc(self): return self
            def is_(self, v): return True

        class _FakeTable:
            class c:
                id = _FakeCol()
                conversation_id = _FakeCol()
                member_id = _FakeCol()
                organization_id = _FakeCol()
                role = _FakeCol()
                content = _FakeCol()
                created_at = _FakeCol()
                parent_message_id = _FakeCol()
                pinned = _FakeCol()
                title = _FakeCol()

        return _FakeTable()

    return _select, _insert, _update, _reflect


# ─── Pin endpoint ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pin_message_toggles_pinned(monkeypatch):
    from routers import chat
    from unittest.mock import AsyncMock, patch

    member = _make_member()
    msg_row = {"id": "msg-1", "conversation_id": "conv-1", "role": "user", "pinned": False}
    conv_row = {"id": "conv-1", "member_id": "member-1", "organization_id": "default"}

    call_idx = [0]
    results = [conv_row, msg_row, None]  # ownership check, message fetch, UPDATE

    class _FakeResult:
        def __init__(self, data):
            self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
                def all(self_inner): return [data] if data else []
            return M()
        def first(self): return self._data
        def scalar_one_or_none(self): return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    _select, _insert, _update, _reflect = _fake_sql_builder()

    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat, "update", _update)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(chat.audit, "log", AsyncMock())

    result = await chat.pin_message("conv-1", "msg-1", member)
    assert result["pinned"] is True
    chat.audit.log.assert_awaited()


@pytest.mark.asyncio
async def test_unpin_message_sets_pinned_false(monkeypatch):
    from routers import chat
    from unittest.mock import AsyncMock

    member = _make_member()
    msg_row = {"id": "msg-1", "conversation_id": "conv-1", "role": "user", "pinned": True}
    conv_row = {"id": "conv-1", "member_id": "member-1", "organization_id": "default"}

    call_idx = [0]
    results = [conv_row, msg_row, None]

    class _FakeResult:
        def __init__(self, data):
            self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
            return M()
        def first(self): return self._data
        def scalar_one_or_none(self): return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    _select, _insert, _update, _reflect = _fake_sql_builder()

    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat, "update", _update)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(chat.audit, "log", AsyncMock())

    result = await chat.unpin_message("conv-1", "msg-1", member)
    assert result["pinned"] is False


@pytest.mark.asyncio
async def test_pin_message_404_wrong_owner(monkeypatch):
    """Cross-member: conversation lookup returns None → 404."""
    from routers import chat
    from unittest.mock import AsyncMock

    member = _make_member(member_id="intruder-99")

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
            return M()
        def first(self): return self._data

    class _FakeConn:
        async def execute(self, stmt): return _FakeResult(None)  # ownership fails
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    _select, _insert, _update, _reflect = _fake_sql_builder()
    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc_info:
        await chat.pin_message("conv-1", "msg-1", member)
    assert exc_info.value.status_code == 404


# ─── Edit endpoint ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_edit_user_message_updates_content(monkeypatch):
    from routers import chat
    from unittest.mock import AsyncMock

    member = _make_member()
    conv_row = {"id": "conv-1", "member_id": "member-1", "organization_id": "default"}
    msg_row = {"id": "msg-1", "conversation_id": "conv-1", "role": "user", "content": "old content"}

    call_idx = [0]
    results = [conv_row, msg_row, None]

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
            return M()
        def first(self): return self._data
        def scalar_one_or_none(self): return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    _select, _insert, _update, _reflect = _fake_sql_builder()
    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat, "update", _update)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(chat.audit, "log", AsyncMock())

    class EditReq:
        content = "new content"

    result = await chat.edit_message("conv-1", "msg-1", EditReq(), member)
    assert result["content"] == "new content"
    chat.audit.log.assert_awaited()
    # Fix 4: audit payload must carry prev/new lengths
    call_kwargs = chat.audit.log.call_args
    assert call_kwargs.kwargs.get("payload") == {"prev_length": len("old content"), "new_length": len("new content")}


@pytest.mark.asyncio
async def test_edit_assistant_message_returns_400(monkeypatch):
    """Editing a non-user message must return 400."""
    from routers import chat
    from unittest.mock import AsyncMock

    member = _make_member()
    conv_row = {"id": "conv-1", "member_id": "member-1", "organization_id": "default"}
    msg_row = {"id": "msg-1", "conversation_id": "conv-1", "role": "assistant", "content": "AI response"}

    call_idx = [0]
    results = [conv_row, msg_row]

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

    _select, _insert, _update, _reflect = _fake_sql_builder()
    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat, "update", _update)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))

    class EditReq:
        content = "hacked content"

    with pytest.raises(HTTPException) as exc_info:
        await chat.edit_message("conv-1", "msg-1", EditReq(), member)
    assert exc_info.value.status_code == 400


# ─── Branch endpoint ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_branch_creates_new_conversation_with_lineage(monkeypatch):
    from routers import chat
    from unittest.mock import AsyncMock

    member = _make_member()
    conv_row = {"id": "conv-1", "member_id": "member-1", "organization_id": "default", "title": "Original"}
    # source message to branch at
    src_msg = {
        "id": "msg-2", "conversation_id": "conv-1", "role": "user",
        "content": "branch here", "created_at": "2024-01-01T00:00:00",
        "organization_id": "default", "region": "us", "token_count": 2,
        "model": None, "mode": None, "citations": [], "tool_traces": [],
        "memory_refs": [], "artifact_refs": [], "approval_state": None,
        "runtime_status": None, "pinned": False,
    }
    prior_msgs = [
        {
            "id": "msg-1", "conversation_id": "conv-1", "role": "user",
            "content": "first", "created_at": "2024-01-01T00:00:00",
            "organization_id": "default", "region": "us", "token_count": 1,
            "model": None, "mode": None, "citations": [], "tool_traces": [],
            "memory_refs": [], "artifact_refs": [], "approval_state": None,
            "runtime_status": None, "pinned": False,
        },
        src_msg,
    ]

    new_conv_id = "new-conv-abc"
    call_idx = [0]
    results = [conv_row, src_msg, prior_msgs, new_conv_id, None]  # ownership, src, prior msgs, new conv INSERT, bulk INSERT

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data[0] if isinstance(data, list) and data else data
                def all(self_inner): return data if isinstance(data, list) else [data]
            return M()
        def first(self): return self._data[0] if isinstance(self._data, list) and self._data else self._data
        def scalar_one(self): return new_conv_id
        def scalar_one_or_none(self): return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    _select, _insert, _update, _reflect = _fake_sql_builder()
    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat, "insert", _insert)
    monkeypatch.setattr(chat, "update", _update)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(chat.audit, "log", AsyncMock())

    result = await chat.branch_conversation("conv-1", "msg-2", member)
    assert "conversation_id" in result
    chat.audit.log.assert_awaited()


@pytest.mark.asyncio
async def test_branch_404_wrong_owner(monkeypatch):
    from routers import chat
    from unittest.mock import AsyncMock

    member = _make_member(member_id="stranger")

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
            return M()
        def first(self): return self._data

    class _FakeConn:
        async def execute(self, stmt): return _FakeResult(None)  # ownership fails
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    _select, _insert, _update, _reflect = _fake_sql_builder()
    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc_info:
        await chat.branch_conversation("conv-1", "msg-2", member)
    assert exc_info.value.status_code == 404


# ─── Save-memory endpoint ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_memory_calls_create_memory_entry(monkeypatch):
    from routers import chat
    from unittest.mock import AsyncMock, patch

    member = _make_member()
    conv_row = {"id": "conv-1", "member_id": "member-1", "organization_id": "default"}
    msg_row = {"id": "msg-1", "conversation_id": "conv-1", "role": "assistant", "content": "Remember this fact."}

    call_idx = [0]
    results = [conv_row, msg_row]

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

    _select, _insert, _update, _reflect = _fake_sql_builder()
    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(chat.audit, "log", AsyncMock())

    create_mem_mock = AsyncMock(return_value="mem-entry-123")
    monkeypatch.setattr(chat, "create_memory_entry", create_mem_mock)

    class SaveMemReq:
        scope = "org"

    result = await chat.save_message_to_memory("conv-1", "msg-1", SaveMemReq(), member)

    assert result["memory_entry_id"] == "mem-entry-123"
    create_mem_mock.assert_awaited_once()
    call_kwargs = create_mem_mock.call_args
    assert call_kwargs.kwargs["content"] == "Remember this fact."
    assert call_kwargs.kwargs["source"] == "explicit"
    assert call_kwargs.kwargs["scope"] == "org"
    assert call_kwargs.kwargs["importance_score"] == 0.8


@pytest.mark.asyncio
async def test_save_memory_personal_scope_uses_member_id(monkeypatch):
    from routers import chat
    from unittest.mock import AsyncMock

    member = _make_member(member_id="member-42", org_id="org-1")
    conv_row = {"id": "conv-1", "member_id": "member-42", "organization_id": "org-1"}
    msg_row = {"id": "msg-1", "conversation_id": "conv-1", "role": "user", "content": "Personal note."}

    call_idx = [0]
    results = [conv_row, msg_row]

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

    _select, _insert, _update, _reflect = _fake_sql_builder()
    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(chat.audit, "log", AsyncMock())

    create_mem_mock = AsyncMock(return_value="mem-personal-456")
    monkeypatch.setattr(chat, "create_memory_entry", create_mem_mock)

    class SaveMemReq:
        scope = "personal"

    result = await chat.save_message_to_memory("conv-1", "msg-1", SaveMemReq(), member)
    call_kwargs = create_mem_mock.call_args
    assert call_kwargs.kwargs["scope"] == "personal"
    assert call_kwargs.kwargs["scope_id"] == "member-42"


# ─── Convert-task endpoint ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_convert_task_calls_create_task_record(monkeypatch):
    from routers import chat
    from unittest.mock import AsyncMock

    member = _make_member()
    conv_row = {"id": "conv-1", "member_id": "member-1", "organization_id": "default"}
    msg_row = {"id": "msg-1", "conversation_id": "conv-1", "role": "user", "content": "Do this task."}

    call_idx = [0]
    results = [conv_row, msg_row]

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

    _select, _insert, _update, _reflect = _fake_sql_builder()
    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(chat.audit, "log", AsyncMock())

    create_task_mock = AsyncMock(return_value="task-xyz-789")
    monkeypatch.setattr(chat, "create_task_record", create_task_mock)

    class ConvertReq:
        model = None
        mode = None

    result = await chat.convert_message_to_task("conv-1", "msg-1", ConvertReq(), member)
    assert result["task_id"] == "task-xyz-789"
    create_task_mock.assert_awaited_once()
    call_kwargs = create_task_mock.call_args
    assert call_kwargs.kwargs["goal"] == "Do this task."
    assert call_kwargs.kwargs["triggered_by"] == "conv-1"


@pytest.mark.asyncio
async def test_convert_task_404_wrong_org(monkeypatch):
    from routers import chat
    from unittest.mock import AsyncMock

    member = _make_member(org_id="org-wrong")

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
            return M()
        def first(self): return self._data

    class _FakeConn:
        async def execute(self, stmt): return _FakeResult(None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    _select, _insert, _update, _reflect = _fake_sql_builder()
    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))

    class ConvertReq:
        model = None
        mode = None

    with pytest.raises(HTTPException) as exc_info:
        await chat.convert_message_to_task("conv-1", "msg-1", ConvertReq(), member)
    assert exc_info.value.status_code == 404


# ─── Fix 1: ownership 404 for edit and save-memory ───────────────────────────

@pytest.mark.asyncio
async def test_edit_message_404_wrong_owner(monkeypatch):
    """Cross-member: conversation lookup returns None → 404 for edit."""
    from routers import chat
    from unittest.mock import AsyncMock

    member = _make_member(member_id="intruder-99")

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
            return M()
        def first(self): return self._data

    class _FakeConn:
        async def execute(self, stmt): return _FakeResult(None)  # ownership fails
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    _select, _insert, _update, _reflect = _fake_sql_builder()
    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))

    class EditReq:
        content = "hacked content"

    with pytest.raises(HTTPException) as exc_info:
        await chat.edit_message("conv-1", "msg-1", EditReq(), member)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_save_memory_404_wrong_owner(monkeypatch):
    """Cross-member: conversation lookup returns None → 404 for save-memory."""
    from routers import chat
    from unittest.mock import AsyncMock

    member = _make_member(member_id="intruder-99")

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data
            return M()
        def first(self): return self._data

    class _FakeConn:
        async def execute(self, stmt): return _FakeResult(None)  # ownership fails
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    _select, _insert, _update, _reflect = _fake_sql_builder()
    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))

    class SaveMemReq:
        scope = "org"

    with pytest.raises(HTTPException) as exc_info:
        await chat.save_message_to_memory("conv-1", "msg-1", SaveMemReq(), member)
    assert exc_info.value.status_code == 404


# ─── Fix 2: or_ used in branch predicate ─────────────────────────────────────

@pytest.mark.asyncio
async def test_branch_uses_or_predicate(monkeypatch):
    """Branch handler must call or_() to build a deterministic WHERE predicate."""
    from routers import chat
    from unittest.mock import AsyncMock, MagicMock

    member = _make_member()
    conv_row = {"id": "conv-1", "member_id": "member-1", "organization_id": "default", "title": "Original"}
    src_msg = {
        "id": "msg-2", "conversation_id": "conv-1", "role": "user",
        "content": "branch here", "created_at": "2024-01-01T00:00:00",
        "organization_id": "default", "region": "us", "token_count": 2,
        "model": None, "mode": None, "citations": [], "tool_traces": [],
        "memory_refs": [], "artifact_refs": [], "approval_state": None,
        "runtime_status": None, "pinned": False,
    }
    prior_msgs = [src_msg]
    new_conv_id = "new-conv-xyz"

    call_idx = [0]
    results = [conv_row, src_msg, prior_msgs, new_conv_id, None]

    class _FakeResult:
        def __init__(self, data): self._data = data
        def mappings(self):
            data = self._data
            class M:
                def first(self_inner): return data[0] if isinstance(data, list) and data else data
                def all(self_inner): return data if isinstance(data, list) else [data]
            return M()
        def first(self): return self._data[0] if isinstance(self._data, list) and self._data else self._data
        def scalar_one(self): return new_conv_id
        def scalar_one_or_none(self): return self._data

    class _FakeConn:
        async def execute(self, stmt):
            idx = call_idx[0]; call_idx[0] += 1
            return _FakeResult(results[idx] if idx < len(results) else None)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    _select, _insert, _update, _reflect = _fake_sql_builder()
    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", _select)
    monkeypatch.setattr(chat, "insert", _insert)
    monkeypatch.setattr(chat, "update", _update)
    monkeypatch.setattr(chat.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(chat.audit, "log", AsyncMock())

    or_spy = MagicMock(wraps=chat.or_)
    monkeypatch.setattr(chat, "or_", or_spy)

    await chat.branch_conversation("conv-1", "msg-2", member)

    assert or_spy.called, "branch_conversation must call or_() for deterministic predicate"
