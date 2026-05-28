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

    async def fake_find_skills(message):
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

    async def fake_find_skills(message):
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

    async def fake_find_skills(message):
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

    async def fake_find_skills(message):
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

    async def fake_create_conversation(member, title, project_id=None):
        return "conv-new-1"

    async def fake_save_message(*args, **kwargs):
        return None

    def fake_extract_explicit(message):
        return None

    async def fake_classify_intent(message):
        return {"mode": "chat", "confidence": 0.9, "goal": ""}

    async def fake_stream_completion(context, model_id=None):
        yield "hello"

    async def fake_extract_and_save(*args, **kwargs):
        pass

    monkeypatch.setattr(chat, "assemble_context", fake_assemble_context)
    monkeypatch.setattr(chat.permissions, "check", fake_check)
    monkeypatch.setattr(chat, "_create_conversation", fake_create_conversation)
    monkeypatch.setattr(chat, "_save_message", fake_save_message)
    monkeypatch.setattr(chat, "extract_explicit_memory_content", fake_extract_explicit)
    monkeypatch.setattr(chat, "classify_intent", fake_classify_intent)
    monkeypatch.setattr(chat, "stream_completion", fake_stream_completion)
    monkeypatch.setattr(chat, "extract_and_save", fake_extract_and_save)
    monkeypatch.setattr(chat.audit, "log", AsyncMock())
    # Force trivial so it lands on the fast path that calls assemble_context
    monkeypatch.setattr(chat, "_is_trivial_chat", lambda msg: True)

    class FakeReq:
        message = "hi"
        conversation_id = None
        model = None
        mode = None
        persona_id = None
        workspace_id = None
        project_id = "proj-from-req"

    import asyncio
    response = await chat.send_message(FakeReq(), member)
    # Drain the streaming response to ensure generator runs
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else chunk.encode()

    assert captured_context.get("project_id") == "proj-from-req"


@pytest.mark.asyncio
async def test_send_message_hydrates_project_id_from_existing_conversation(monkeypatch):
    """When req.project_id is absent but the conversation has one, it is hydrated."""
    from routers import chat
    from core.models import Member

    member = Member(id="member-1", organization_id="default", email="test@example.com", role="user")

    captured_context = {}

    async def fake_assemble_context(conversation_id, message, requester_context):
        captured_context["project_id"] = requester_context.project_id
        return [{"role": "system", "content": "sys"}, {"role": "user", "content": message}]

    async def fake_check(*args, **kwargs):
        return True

    async def fake_save_message(*args, **kwargs):
        return None

    def fake_extract_explicit(message):
        return None

    async def fake_classify_intent(message):
        return {"mode": "chat", "confidence": 0.9, "goal": ""}

    async def fake_stream_completion(context, model_id=None):
        yield "hello"

    async def fake_extract_and_save(*args, **kwargs):
        pass

    # Fake reflect_table: returns a table stub for both conversations and messages
    class _FakeCol:
        def __eq__(self, other): return True
        def __and__(self, other): return True

    class _FakeConversations:
        class c:
            id = _FakeCol()
            member_id = _FakeCol()
            organization_id = _FakeCol()
            project_id = _FakeCol()
            role = _FakeCol()
            content = _FakeCol()
            conversation_id = _FakeCol()
            created_at = _FakeCol()

    async def fake_reflect_table(name):
        return _FakeConversations()

    # DB call for project_id hydration
    class _FakeResult:
        def __init__(self, data):
            self._data = data

        def mappings(self):
            return self

        def first(self):
            return self._data

        def all(self):
            if isinstance(self._data, list):
                return self._data
            return [self._data] if self._data else []

    class _FakeConn:
        async def execute(self, stmt):
            return _FakeResult({"project_id": "proj-from-db"})

        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    class _FakeEngine:
        def begin(self): return _FakeConn()

    class _FakeSelect:
        def where(self, *args): return self
        def order_by(self, *args): return self
        def limit(self, *args): return self

    monkeypatch.setattr(chat, "assemble_context", fake_assemble_context)
    monkeypatch.setattr(chat.permissions, "check", fake_check)
    monkeypatch.setattr(chat, "_save_message", fake_save_message)
    monkeypatch.setattr(chat, "extract_explicit_memory_content", fake_extract_explicit)
    monkeypatch.setattr(chat, "classify_intent", fake_classify_intent)
    monkeypatch.setattr(chat, "stream_completion", fake_stream_completion)
    monkeypatch.setattr(chat, "extract_and_save", fake_extract_and_save)
    monkeypatch.setattr(chat.audit, "log", AsyncMock())
    monkeypatch.setattr(chat, "_is_trivial_chat", lambda msg: True)
    monkeypatch.setattr(chat, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "select", lambda *args: _FakeSelect())

    class FakeReq:
        message = "hi"
        conversation_id = "conv-existing-1"
        model = None
        mode = None
        persona_id = None
        workspace_id = None
        project_id = None  # not set in the request

    response = await chat.send_message(FakeReq(), member)
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else chunk.encode()

    assert captured_context.get("project_id") == "proj-from-db"
