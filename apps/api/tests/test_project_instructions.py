"""Project instructions context layer — TDD tests.

Tests are written first (failing); implementation follows.

Covers:
- _load_project_instructions helper: returns text, None for wrong org / missing / empty
- assemble_context: includes the block only when project_id is set + org matches
- chat send_message: req.project_id is pushed into requester_context.project_id
- chat send_message: project_id is hydrated from an existing conversation when req
  does not include it
"""
import pytest
from unittest.mock import AsyncMock


# ─── _load_project_instructions ───────────────────────────────────────────────

def _make_project_table_and_engine(project_row):
    """Shared helper: build fake engine + reflect for _load_project_instructions tests."""
    class _FakeCol:
        def __eq__(self, other): return True

    class _FakeTable:
        class c:
            id = _FakeCol()
            organization_id = _FakeCol()
            instructions = _FakeCol()

    class _FakeResult:
        def mappings(self_inner):
            return self_inner
        def first(self_inner):
            return project_row

    class _FakeConn:
        async def execute(self, stmt):
            return _FakeResult()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    class _FakeSelect:
        def where(self, *args): return self

    async def _reflect(_name):
        return _FakeTable()

    return _FakeEngine(), _reflect, lambda *args: _FakeSelect()


@pytest.mark.asyncio
async def test_load_project_instructions_returns_text_for_valid_project(monkeypatch):
    """Returns the instructions string when the project belongs to the caller's org."""
    from core import context

    project_row = {
        "id": "proj-1",
        "organization_id": "default",
        "instructions": "Always reply in bullet points.",
    }
    engine, reflect, sel = _make_project_table_and_engine(project_row)
    monkeypatch.setattr(context, "engine", engine)
    monkeypatch.setattr(context, "reflect_table", reflect)
    monkeypatch.setattr(context, "select", sel)

    result = await context._load_project_instructions("proj-1", "default")
    assert result == "Always reply in bullet points."


@pytest.mark.asyncio
async def test_load_project_instructions_returns_none_when_project_not_found(monkeypatch):
    """Returns None when the project row is missing (cross-org or non-existent)."""
    from core import context

    engine, reflect, sel = _make_project_table_and_engine(None)
    monkeypatch.setattr(context, "engine", engine)
    monkeypatch.setattr(context, "reflect_table", reflect)
    monkeypatch.setattr(context, "select", sel)

    result = await context._load_project_instructions("proj-unknown", "default")
    assert result is None


@pytest.mark.asyncio
async def test_load_project_instructions_returns_none_when_instructions_empty(monkeypatch):
    """Returns None when instructions column is an empty string."""
    from core import context

    project_row = {"id": "proj-1", "organization_id": "default", "instructions": ""}
    engine, reflect, sel = _make_project_table_and_engine(project_row)
    monkeypatch.setattr(context, "engine", engine)
    monkeypatch.setattr(context, "reflect_table", reflect)
    monkeypatch.setattr(context, "select", sel)

    result = await context._load_project_instructions("proj-1", "default")
    assert result is None


@pytest.mark.asyncio
async def test_load_project_instructions_returns_none_when_instructions_null(monkeypatch):
    """Returns None when instructions column is NULL."""
    from core import context

    project_row = {"id": "proj-1", "organization_id": "default", "instructions": None}
    engine, reflect, sel = _make_project_table_and_engine(project_row)
    monkeypatch.setattr(context, "engine", engine)
    monkeypatch.setattr(context, "reflect_table", reflect)
    monkeypatch.setattr(context, "select", sel)

    result = await context._load_project_instructions("proj-1", "default")
    assert result is None


# ─── assemble_context: project instructions block ─────────────────────────────

@pytest.mark.asyncio
async def test_assemble_context_includes_project_instructions_block(monkeypatch):
    """# Project Instructions block appears in system prompt when project_id is set."""
    from core import context
    from core.models import RequesterContext

    async def fake_org_context(org_id):
        return ""

    async def fake_find_skills(message, *args, **kwargs):
        return []

    async def fake_retrieve(message, requester_context):
        return []

    async def fake_load_project_instructions(project_id, org_id):
        assert project_id == "proj-1"
        assert org_id == "default"
        return "Respond in formal English only."

    class _FakeResult:
        def mappings(self):
            return self
        def all(self):
            return []

    class _FakeConn:
        async def execute(self, stmt):
            return _FakeResult()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    class _FakeColumn:
        def __eq__(self, other): return ("eq", other)
        def desc(self): return ("desc", self)

    class _FakeMessages:
        class c:
            role = _FakeColumn()
            content = _FakeColumn()
            conversation_id = _FakeColumn()
            created_at = _FakeColumn()

    async def fake_reflect_table(name):
        return _FakeMessages()

    class _FakeSelect:
        def where(self, *args): return self
        def order_by(self, *args): return self
        def limit(self, *args): return self

    monkeypatch.setattr(context, "load_org_context", fake_org_context)
    async def _fake_candidates(org_id):
        return []
    monkeypatch.setattr(context, "get_candidate_skills", _fake_candidates)
    monkeypatch.setattr(context, "find_relevant_skills", fake_find_skills)
    monkeypatch.setattr(context.memory, "retrieve", fake_retrieve)
    monkeypatch.setattr(context, "_load_project_instructions", fake_load_project_instructions)
    monkeypatch.setattr(context, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(context, "engine", _FakeEngine())
    monkeypatch.setattr(context, "select", lambda *args: _FakeSelect())

    assembled = await context.assemble_context(
        "conversation-1",
        "hello",
        RequesterContext(member_id="member-1", org_id="default", project_id="proj-1"),
    )

    system = assembled[0]["content"]
    assert "# Project Instructions\nRespond in formal English only." in system


@pytest.mark.asyncio
async def test_assemble_context_omits_project_instructions_when_no_project_id(monkeypatch):
    """# Project Instructions block is absent when project_id is None."""
    from core import context
    from core.models import RequesterContext

    helper_called = [False]

    async def fake_org_context(org_id):
        return ""

    async def fake_find_skills(message, *args, **kwargs):
        return []

    async def fake_retrieve(message, requester_context):
        return []

    async def fake_load_project_instructions(project_id, org_id):
        helper_called[0] = True
        return "This should not appear."

    class _FakeResult:
        def mappings(self):
            return self
        def all(self):
            return []

    class _FakeConn:
        async def execute(self, stmt):
            return _FakeResult()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    class _FakeColumn:
        def __eq__(self, other): return ("eq", other)
        def desc(self): return ("desc", self)

    class _FakeMessages:
        class c:
            role = _FakeColumn()
            content = _FakeColumn()
            conversation_id = _FakeColumn()
            created_at = _FakeColumn()

    async def fake_reflect_table(name):
        return _FakeMessages()

    class _FakeSelect:
        def where(self, *args): return self
        def order_by(self, *args): return self
        def limit(self, *args): return self

    monkeypatch.setattr(context, "load_org_context", fake_org_context)
    async def _fake_candidates(org_id):
        return []
    monkeypatch.setattr(context, "get_candidate_skills", _fake_candidates)
    monkeypatch.setattr(context, "find_relevant_skills", fake_find_skills)
    monkeypatch.setattr(context.memory, "retrieve", fake_retrieve)
    monkeypatch.setattr(context, "_load_project_instructions", fake_load_project_instructions)
    monkeypatch.setattr(context, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(context, "engine", _FakeEngine())
    monkeypatch.setattr(context, "select", lambda *args: _FakeSelect())

    assembled = await context.assemble_context(
        "conversation-1",
        "hello",
        RequesterContext(member_id="member-1", org_id="default"),  # no project_id
    )

    system = assembled[0]["content"]
    assert "# Project Instructions" not in system
    assert not helper_called[0], "_load_project_instructions must not be called when project_id is None"


@pytest.mark.asyncio
async def test_assemble_context_project_instructions_before_skills(monkeypatch):
    """Project Instructions block appears BEFORE any Skill block in the system prompt."""
    from core import context
    from core.models import RequesterContext

    async def fake_org_context(org_id):
        return ""

    async def fake_find_skills(message, *args, **kwargs):
        return ["general"]

    async def fake_load_skill(skill_id, **kwargs):
        return "Skill content here."

    async def fake_retrieve(message, requester_context):
        return []

    async def fake_load_project_instructions(project_id, org_id):
        return "Project rule: be concise."

    class _FakeResult:
        def mappings(self):
            return self
        def all(self):
            return []

    class _FakeConn:
        async def execute(self, stmt):
            return _FakeResult()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    class _FakeColumn:
        def __eq__(self, other): return ("eq", other)
        def desc(self): return ("desc", self)

    class _FakeMessages:
        class c:
            role = _FakeColumn()
            content = _FakeColumn()
            conversation_id = _FakeColumn()
            created_at = _FakeColumn()

    async def fake_reflect_table(name):
        return _FakeMessages()

    class _FakeSelect:
        def where(self, *args): return self
        def order_by(self, *args): return self
        def limit(self, *args): return self

    monkeypatch.setattr(context, "load_org_context", fake_org_context)
    async def _fake_candidates(org_id):
        return []
    monkeypatch.setattr(context, "get_candidate_skills", _fake_candidates)
    monkeypatch.setattr(context, "find_relevant_skills", fake_find_skills)
    monkeypatch.setattr(context, "load_skill_content", fake_load_skill)
    monkeypatch.setattr(context.memory, "retrieve", fake_retrieve)
    monkeypatch.setattr(context, "_load_project_instructions", fake_load_project_instructions)
    monkeypatch.setattr(context, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(context, "engine", _FakeEngine())
    monkeypatch.setattr(context, "select", lambda *args: _FakeSelect())

    assembled = await context.assemble_context(
        "conversation-1",
        "help me",
        RequesterContext(member_id="member-1", org_id="default", project_id="proj-1"),
    )

    system = assembled[0]["content"]
    proj_pos = system.find("# Project Instructions")
    skill_pos = system.find("# Skill:")
    assert proj_pos != -1, "# Project Instructions must be present"
    assert skill_pos != -1, "# Skill: must be present"
    assert proj_pos < skill_pos, "Project Instructions must appear before Skill blocks"


@pytest.mark.asyncio
async def test_assemble_context_omits_project_block_when_instructions_none(monkeypatch):
    """# Project Instructions block is omitted when helper returns None (e.g. cross-org)."""
    from core import context
    from core.models import RequesterContext

    async def fake_org_context(org_id):
        return ""

    async def fake_find_skills(message, *args, **kwargs):
        return []

    async def fake_retrieve(message, requester_context):
        return []

    async def fake_load_project_instructions(project_id, org_id):
        return None  # project not found in this org

    class _FakeResult:
        def mappings(self):
            return self
        def all(self):
            return []

    class _FakeConn:
        async def execute(self, stmt):
            return _FakeResult()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    class _FakeColumn:
        def __eq__(self, other): return ("eq", other)
        def desc(self): return ("desc", self)

    class _FakeMessages:
        class c:
            role = _FakeColumn()
            content = _FakeColumn()
            conversation_id = _FakeColumn()
            created_at = _FakeColumn()

    async def fake_reflect_table(name):
        return _FakeMessages()

    class _FakeSelect:
        def where(self, *args): return self
        def order_by(self, *args): return self
        def limit(self, *args): return self

    monkeypatch.setattr(context, "load_org_context", fake_org_context)
    async def _fake_candidates(org_id):
        return []
    monkeypatch.setattr(context, "get_candidate_skills", _fake_candidates)
    monkeypatch.setattr(context, "find_relevant_skills", fake_find_skills)
    monkeypatch.setattr(context.memory, "retrieve", fake_retrieve)
    monkeypatch.setattr(context, "_load_project_instructions", fake_load_project_instructions)
    monkeypatch.setattr(context, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(context, "engine", _FakeEngine())
    monkeypatch.setattr(context, "select", lambda *args: _FakeSelect())

    assembled = await context.assemble_context(
        "conversation-1",
        "hello",
        RequesterContext(member_id="member-1", org_id="default", project_id="proj-orphan"),
    )

    system = assembled[0]["content"]
    assert "# Project Instructions" not in system


# ─── chat send_message: project_id hydration ──────────────────────────────────

@pytest.mark.asyncio
async def test_send_message_pushes_req_project_id_into_requester_context(monkeypatch):
    """req.project_id is set in requester_context.project_id before assemble_context."""
    from routers import chat
    from core.models import Member

    member = Member(id="member-1", organization_id="default", email="test@example.com", role="user")

    captured_context = {}

    async def fake_assemble_context(conversation_id, message, requester_context):
        captured_context["project_id"] = requester_context.project_id
        return [{"role": "system", "content": "sys"}, {"role": "user", "content": message}]

    async def fake_check(*args, **kwargs):
        return True

    async def fake_project_access(*args, **kwargs):
        return True

    async def fake_create_conversation(
        member, title, project_id=None, workspace_id=None
    ):
        return "conv-new-1"

    async def fake_workspace_access(*args, **kwargs):
        return {"id": "workspace-default"}

    async def fake_save_message(*args, **kwargs):
        # New conversations (conversation_id=None) return None (no RETURNING fired)
        return None

    def fake_extract_explicit(message):
        return None


    async def fake_stream_chat_turn(**kwargs):
        yield {"type": "done"}


    monkeypatch.setattr(chat, "assemble_context", fake_assemble_context)
    monkeypatch.setattr(chat.permissions, "check", fake_check)
    monkeypatch.setattr(chat, "member_can_edit_project", fake_project_access)
    monkeypatch.setattr(
        chat.workspace_access, "require_workspace_access", fake_workspace_access
    )
    monkeypatch.setattr(chat, "_create_conversation", fake_create_conversation)
    monkeypatch.setattr(chat, "_save_message", fake_save_message)
    monkeypatch.setattr(chat, "extract_explicit_memory_content", fake_extract_explicit)
    monkeypatch.setattr(chat, "stream_chat_turn", fake_stream_chat_turn)
    monkeypatch.setattr(chat.audit, "log", AsyncMock())

    class FakeReq:
        message = "hi"
        conversation_id = None
        model = "gpt-5.4-mini"
        reasoning_effort = None
        mode = None
        persona_id = None
        workspace_id = None
        project_id = "proj-from-req"

    response = await chat.send_message(FakeReq(), member)
    # Drain the streaming response to ensure generator runs
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else chunk.encode()

    assert captured_context.get("project_id") == "proj-from-req"


@pytest.mark.asyncio
async def test_send_message_hydrates_project_id_from_existing_conversation(monkeypatch):
    """When req.project_id is absent, project_id is hydrated from _save_message's return value.

    With fix #1, _save_message returns the conversation row dict (including project_id)
    via RETURNING — no extra roundtrip or reflect_table call is needed.
    """
    from routers import chat
    from core.models import Member

    member = Member(id="member-1", organization_id="default", email="test@example.com", role="user")

    captured_context = {}

    async def fake_assemble_context(conversation_id, message, requester_context):
        captured_context["project_id"] = requester_context.project_id
        return [{"role": "system", "content": "sys"}, {"role": "user", "content": message}]

    async def fake_check(*args, **kwargs):
        return True

    async def fake_project_access(*args, **kwargs):
        return True

    async def fake_workspace_access(*args, **kwargs):
        return {"id": "workspace-default"}

    async def fake_save_message(*args, **kwargs):
        # Simulate _save_message returning the conversation row when member/org are scoped.
        if kwargs.get("_member_id") is not None:
            return {"project_id": "proj-from-db"}
        return None

    def fake_extract_explicit(message):
        return None


    async def fake_stream_chat_turn(**kwargs):
        yield {"type": "done"}


    monkeypatch.setattr(chat, "assemble_context", fake_assemble_context)
    monkeypatch.setattr(chat.permissions, "check", fake_check)
    monkeypatch.setattr(chat, "member_can_edit_project", fake_project_access)
    monkeypatch.setattr(
        chat.workspace_access, "require_workspace_access", fake_workspace_access
    )
    monkeypatch.setattr(chat, "_save_message", fake_save_message)
    monkeypatch.setattr(chat, "extract_explicit_memory_content", fake_extract_explicit)
    monkeypatch.setattr(chat, "stream_chat_turn", fake_stream_chat_turn)
    monkeypatch.setattr(chat.audit, "log", AsyncMock())

    class FakeReq:
        message = "hi"
        conversation_id = "conv-existing-1"
        model = "gpt-5.4-mini"
        reasoning_effort = None
        mode = None
        persona_id = None
        workspace_id = None
        project_id = None  # not set in the request

    response = await chat.send_message(FakeReq(), member)
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else chunk.encode()

    assert captured_context.get("project_id") == "proj-from-db"


# ─── Fix #2: whitespace-only instructions ─────────────────────────────────────

@pytest.mark.asyncio
async def test_load_project_instructions_returns_none_when_instructions_whitespace_only(monkeypatch):
    """Returns None when instructions column contains only whitespace."""
    from core import context

    project_row = {"id": "proj-1", "organization_id": "default", "instructions": "   \n  "}
    engine, reflect, sel = _make_project_table_and_engine(project_row)
    monkeypatch.setattr(context, "engine", engine)
    monkeypatch.setattr(context, "reflect_table", reflect)
    monkeypatch.setattr(context, "select", sel)

    result = await context._load_project_instructions("proj-1", "default")
    assert result is None


@pytest.mark.asyncio
async def test_assemble_context_omits_project_block_when_instructions_whitespace_only(monkeypatch):
    """# Project Instructions block is absent when helper returns None for whitespace-only instructions."""
    from core import context
    from core.models import RequesterContext

    async def fake_org_context(org_id):
        return ""

    async def fake_find_skills(message, *args, **kwargs):
        return []

    async def fake_retrieve(message, requester_context):
        return []

    async def fake_load_project_instructions(project_id, org_id):
        # Simulate what the real helper does for whitespace-only instructions
        return None

    class _FakeResult:
        def mappings(self):
            return self
        def all(self):
            return []

    class _FakeConn:
        async def execute(self, stmt):
            return _FakeResult()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    class _FakeColumn:
        def __eq__(self, other): return ("eq", other)
        def desc(self): return ("desc", self)

    class _FakeMessages:
        class c:
            role = _FakeColumn()
            content = _FakeColumn()
            conversation_id = _FakeColumn()
            created_at = _FakeColumn()

    async def fake_reflect_table(name):
        return _FakeMessages()

    class _FakeSelect:
        def where(self, *args): return self
        def order_by(self, *args): return self
        def limit(self, *args): return self

    monkeypatch.setattr(context, "load_org_context", fake_org_context)
    async def _fake_candidates(org_id):
        return []
    monkeypatch.setattr(context, "get_candidate_skills", _fake_candidates)
    monkeypatch.setattr(context, "find_relevant_skills", fake_find_skills)
    monkeypatch.setattr(context.memory, "retrieve", fake_retrieve)
    monkeypatch.setattr(context, "_load_project_instructions", fake_load_project_instructions)
    monkeypatch.setattr(context, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(context, "engine", _FakeEngine())
    monkeypatch.setattr(context, "select", lambda *args: _FakeSelect())

    assembled = await context.assemble_context(
        "conversation-1",
        "hello",
        RequesterContext(member_id="member-1", org_id="default", project_id="proj-1"),
    )

    system = assembled[0]["content"]
    assert "# Project Instructions" not in system


# ─── Fix #3: caller project_id vs conversation mismatch → 422 ─────────────────

@pytest.mark.asyncio
async def test_send_message_raises_422_when_project_id_mismatches_conversation(monkeypatch):
    """When req.project_id and the conversation's stored project_id differ, 422 is raised."""
    from routers import chat
    from core.models import Member
    from fastapi import HTTPException

    member = Member(id="member-1", organization_id="default", email="test@example.com", role="user")

    async def fake_check(*args, **kwargs):
        return True

    async def fake_workspace_access(*args, **kwargs):
        return {"id": "workspace-default"}

    async def fake_save_message(*args, **kwargs):
        # Return a stored project_id that differs from req.project_id
        if kwargs.get("_member_id") is not None:
            return {"project_id": "proj-stored"}
        return None

    monkeypatch.setattr(chat.permissions, "check", fake_check)
    monkeypatch.setattr(
        chat.workspace_access, "require_workspace_access", fake_workspace_access
    )
    monkeypatch.setattr(chat, "_save_message", fake_save_message)
    monkeypatch.setattr(chat.audit, "log", AsyncMock())

    class FakeReq:
        message = "hi"
        conversation_id = "conv-existing-1"
        model = "gpt-5.4-mini"
        reasoning_effort = None
        mode = None
        persona_id = None
        workspace_id = None
        project_id = "proj-different"  # mismatch with "proj-stored"

    with pytest.raises(HTTPException) as exc_info:
        await chat.send_message(FakeReq(), member)

    assert exc_info.value.status_code == 422
    assert "project_id does not match conversation" in exc_info.value.detail


# ─── Fix #5: no hydration when both project_id and conversation_id are None ───

@pytest.mark.asyncio
async def test_send_message_skips_hydration_when_no_conversation_id(monkeypatch):
    """When both req.project_id and req.conversation_id are None, _save_message
    is called without _member_id/_org_id (so no RETURNING fires) and
    requester_context.project_id remains None.
    """
    from routers import chat
    from core.models import Member

    member = Member(id="member-1", organization_id="default", email="test@example.com", role="user")

    save_message_kwargs_list: list[dict] = []
    captured_context: dict = {}

    async def fake_assemble_context(conversation_id, message, requester_context):
        captured_context["project_id"] = requester_context.project_id
        return [{"role": "system", "content": "sys"}, {"role": "user", "content": message}]

    async def fake_check(*args, **kwargs):
        return True

    async def fake_create_conversation(
        member, title, project_id=None, workspace_id=None
    ):
        return "conv-new-1"

    async def fake_workspace_access(*args, **kwargs):
        return {"id": "workspace-default"}

    async def fake_save_message(*args, **kwargs):
        save_message_kwargs_list.append(kwargs)
        return None  # new conversation: no RETURNING

    def fake_extract_explicit(message):
        return None


    async def fake_stream_chat_turn(**kwargs):
        yield {"type": "done"}


    monkeypatch.setattr(chat, "assemble_context", fake_assemble_context)
    monkeypatch.setattr(chat.permissions, "check", fake_check)
    monkeypatch.setattr(
        chat.workspace_access, "require_workspace_access", fake_workspace_access
    )
    monkeypatch.setattr(chat, "_create_conversation", fake_create_conversation)
    monkeypatch.setattr(chat, "_save_message", fake_save_message)
    monkeypatch.setattr(chat, "extract_explicit_memory_content", fake_extract_explicit)
    monkeypatch.setattr(chat, "stream_chat_turn", fake_stream_chat_turn)
    monkeypatch.setattr(chat.audit, "log", AsyncMock())

    class FakeReq:
        message = "hi"
        conversation_id = None   # no existing conversation
        model = "gpt-5.4-mini"
        reasoning_effort = None
        mode = None
        persona_id = None
        workspace_id = None
        project_id = None        # no project_id either

    response = await chat.send_message(FakeReq(), member)
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else chunk.encode()

    # Hydration (the RETURNING / conversation SELECT) only fires when both
    # _member_id and _org_id are supplied; the new-conversation path omits
    # _member_id, so no hydration runs. _org_id is still passed because the
    # message insert is org-scoped.
    assert save_message_kwargs_list, "_save_message was not called"
    user_msg_kwargs = save_message_kwargs_list[0]
    assert user_msg_kwargs.get("_member_id") is None, (
        "_member_id must be None for a new conversation (no hydration path)"
    )
    assert user_msg_kwargs.get("_org_id") == "default", (
        "_org_id is passed for the org-scoped insert even when hydration is skipped"
    )
    # project_id in requester_context must remain None
    assert captured_context.get("project_id") is None


@pytest.mark.asyncio
async def test_project_task_resume_revalidates_membership_before_cached_context(monkeypatch):
    from runtime import agent_loop
    from core import memory_access

    class Col:
        def __eq__(self, other): return True
    class Table:
        class c:
            id = Col(); organization_id = Col(); status = Col()
    class Clause:
        def where(self, *args): return self
    class Result:
        def mappings(self): return self
        def first(self):
            return {"id": "member-1", "organization_id": "default", "email": "m@example.com", "role": "user", "status": "active"}
    class Conn:
        async def execute(self, stmt): return Result()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
    class Engine:
        def begin(self): return Conn()

    async def revoked(*args, **kwargs): return False
    async def reflect(_name): return Table()

    monkeypatch.setattr(agent_loop, "engine", Engine())
    monkeypatch.setattr(agent_loop, "reflect_table", reflect)
    monkeypatch.setattr(agent_loop, "select", lambda *args: Clause())
    monkeypatch.setattr(agent_loop, "_agent_system_message", AsyncMock(return_value={"role": "system", "content": "base"}))
    monkeypatch.setattr(memory_access, "member_can_access_project", revoked)

    task = {
        "id": "task-1",
        "organization_id": "default",
        "triggered_by_member_id": "member-1",
        "triggered_by": "manual",
        "project_id": "project-1",
        "agent_state": {"agent_history": [{"role": "system", "content": "cached private project context"}]},
    }
    with pytest.raises(PermissionError, match="access was revoked"):
        await agent_loop._load_history(task)


@pytest.mark.asyncio
async def test_project_agent_trigger_is_not_treated_as_conversation_uuid(monkeypatch):
    from runtime import agent_loop
    from core import context, memory_access

    class Col:
        def __eq__(self, other): return True
    class Table:
        class c:
            id = Col(); organization_id = Col(); status = Col()
    class Clause:
        def where(self, *args): return self
    class Result:
        def mappings(self): return self
        def first(self):
            return {"id": "member-1", "organization_id": "default", "email": "m@example.com", "role": "user", "status": "active"}
    class Conn:
        async def execute(self, stmt): return Result()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
    class Engine:
        def begin(self): return Conn()

    captured = {}
    async def allowed(*args, **kwargs): return True
    async def assemble(conversation_id, message, requester):
        captured["conversation_id"] = conversation_id
        captured["requester_conversation_id"] = requester.conversation_id
        return [{"role": "system", "content": "fresh authorized context"}]
    async def reflect(_name): return Table()

    monkeypatch.setattr(agent_loop, "engine", Engine())
    monkeypatch.setattr(agent_loop, "reflect_table", reflect)
    monkeypatch.setattr(agent_loop, "select", lambda *args: Clause())
    monkeypatch.setattr(agent_loop, "save_task", AsyncMock())
    monkeypatch.setattr(agent_loop, "_agent_system_message", AsyncMock(return_value={"role": "system", "content": "base"}))
    monkeypatch.setattr(memory_access, "member_can_access_project", allowed)
    monkeypatch.setattr(context, "assemble_context", assemble)

    task = {
        "id": "task-1",
        "organization_id": "default",
        "triggered_by_member_id": "member-1",
        "triggered_by": "agent:profile-1",
        "project_id": "project-1",
        "goal": "prepare project brief",
        "agent_state": {"agent_history": [{"role": "system", "content": "old"}]},
    }
    history = await agent_loop._load_history(task)
    assert captured == {"conversation_id": None, "requester_conversation_id": None}
    assert "fresh authorized context" in history[0]["content"]
