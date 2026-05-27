"""Composer mode selector — TDD tests.

Run failing-first (before implementation), then pass after changes.

Tests:
1. normalize_mode (from core.modes) coerces unknown/None → "default", passes valid modes through,
   lowercases/trims before checking.
2. create_task_record writes (normalized) mode to the task insert row.
3. Fast-path send_message persists chosen mode on the assistant message.
"""
import pytest


# ── 1. normalize_mode unit ───────────────────────────────────────────────────

def test_normalize_mode_none_becomes_default():
    from core.modes import normalize_mode
    assert normalize_mode(None) == "default"


def test_normalize_mode_unknown_becomes_default():
    from core.modes import normalize_mode
    assert normalize_mode("xyz") == "default"


def test_normalize_mode_empty_string_becomes_default():
    from core.modes import normalize_mode
    assert normalize_mode("") == "default"


@pytest.mark.parametrize("mode", [
    "default", "research", "agent", "browser", "computer",
    "data", "image", "voice", "coding",
])
def test_normalize_mode_valid_passes_through(mode):
    from core.modes import normalize_mode
    assert normalize_mode(mode) == mode


def test_normalize_mode_lowercases():
    from core.modes import normalize_mode
    assert normalize_mode("Coding") == "coding"


def test_normalize_mode_trims_whitespace():
    from core.modes import normalize_mode
    assert normalize_mode(" agent ") == "agent"


# ── 2. create_task_record writes mode to tasks insert ─────────────────────────

@pytest.mark.asyncio
async def test_create_task_record_writes_mode(monkeypatch):
    """mode passed to create_task_record must appear in the INSERT values dict."""
    from routers import tasks as tasks_router
    from core.models import Member

    member = Member(
        id="member-1",
        organization_id="default",
        email="admin@example.com",
        role="admin",
    )

    insert_values: dict = {}

    class _FakeInsertClause:
        def values(self, **kwargs):
            insert_values.update(kwargs)
            return self
        def returning(self, _col):
            return self

    class _FakeScalar:
        def scalar_one(self):
            return "task-uuid-123"

    class _FakeConn:
        async def execute(self, stmt):
            return _FakeScalar()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    class _FakeEngine:
        def begin(self):
            return _FakeConn()

    class _FakeTable:
        class c:
            id = None

    def fake_insert(_tbl):
        return _FakeInsertClause()

    async def fake_reflect(_name):
        return _FakeTable()

    async def fake_permission(*args, **kwargs):
        return True

    def fake_resolve_agent_model(model):
        return model or "llama3"

    monkeypatch.setattr(tasks_router, "engine", _FakeEngine())
    monkeypatch.setattr(tasks_router, "reflect_table", fake_reflect)
    monkeypatch.setattr(tasks_router, "insert", fake_insert)
    monkeypatch.setattr(tasks_router.permissions, "check", fake_permission)

    # Patch resolve_agent_model inside the lazy import
    import core.llm as llm_mod
    monkeypatch.setattr(llm_mod, "resolve_agent_model", fake_resolve_agent_model)

    task_id = await tasks_router.create_task_record(
        goal="Test goal",
        member=member,
        triggered_by="conv-1",
        mode="research",
    )

    assert task_id == "task-uuid-123"
    assert insert_values.get("mode") == "research", (
        f"mode missing from task insert; got: {insert_values}"
    )


@pytest.mark.asyncio
async def test_create_task_record_normalizes_unknown_mode(monkeypatch):
    """create_task_record must normalize an unknown mode to 'default'."""
    from routers import tasks as tasks_router
    from core.models import Member

    member = Member(
        id="member-1",
        organization_id="default",
        email="admin@example.com",
        role="admin",
    )

    insert_values: dict = {}

    class _FakeInsertClause:
        def values(self, **kwargs):
            insert_values.update(kwargs)
            return self
        def returning(self, _col):
            return self

    class _FakeScalar:
        def scalar_one(self):
            return "task-uuid-456"

    class _FakeConn:
        async def execute(self, stmt):
            return _FakeScalar()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    class _FakeEngine:
        def begin(self):
            return _FakeConn()

    class _FakeTable:
        class c:
            id = None

    def fake_insert(_tbl):
        return _FakeInsertClause()

    async def fake_reflect(_name):
        return _FakeTable()

    async def fake_permission(*args, **kwargs):
        return True

    def fake_resolve_agent_model(model):
        return model or "llama3"

    monkeypatch.setattr(tasks_router, "engine", _FakeEngine())
    monkeypatch.setattr(tasks_router, "reflect_table", fake_reflect)
    monkeypatch.setattr(tasks_router, "insert", fake_insert)
    monkeypatch.setattr(tasks_router.permissions, "check", fake_permission)

    import core.llm as llm_mod
    monkeypatch.setattr(llm_mod, "resolve_agent_model", fake_resolve_agent_model)

    task_id = await tasks_router.create_task_record(
        goal="Test goal",
        member=member,
        triggered_by="conv-1",
        mode="xyz",  # unknown mode — must be coerced to "default"
    )

    assert task_id == "task-uuid-456"
    assert insert_values.get("mode") == "default", (
        f"Unknown mode was not normalized to 'default'; got: {insert_values.get('mode')!r}"
    )


# ── 3. Fast-path send_message persists request mode on assistant message ──────

@pytest.mark.asyncio
async def test_send_message_fast_path_persists_mode(monkeypatch):
    """On the trivial-chat fast path, the assistant message is saved with
    the mode from the request body, not the hardcoded 'chat' literal.
    """
    from routers import chat
    from core.models import Member

    member = Member(
        id="member-1",
        organization_id="default",
        email="admin@example.com",
        role="admin",
    )

    saved_args: list[dict] = []

    async def fake_save_message(conv_id, role, content, **kwargs):
        if role == "assistant":
            saved_args.append({"conv_id": conv_id, "role": role, **kwargs})

    async def fake_permissions_check(*args, **kwargs):
        return True

    async def fake_create_conversation(member, title):
        return "conv-new"

    async def fake_assemble_context(conv_id, msg, ctx):
        return [{"role": "user", "content": msg}]

    async def fake_stream_completion(context, model_id=None):
        yield "Hello"

    async def fake_extract_and_save(*args, **kwargs):
        pass

    async def fake_extract_explicit(msg):
        return None  # type: ignore

    async def fake_classify_intent(msg):
        return {"mode": "chat"}

    # Force trivial-chat gate so we hit the fast path
    monkeypatch.setattr(chat, "_is_trivial_chat", lambda msg: True)
    monkeypatch.setattr(chat, "_save_message", fake_save_message)
    monkeypatch.setattr(chat.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(chat, "_create_conversation", fake_create_conversation)
    monkeypatch.setattr(chat, "assemble_context", fake_assemble_context)
    monkeypatch.setattr(chat, "stream_completion", fake_stream_completion)
    monkeypatch.setattr(chat, "extract_and_save", fake_extract_and_save)
    monkeypatch.setattr(chat, "extract_explicit_memory_content", lambda msg: None)
    monkeypatch.setattr(chat, "classify_intent", fake_classify_intent)
    monkeypatch.setattr(chat, "normalize_chat_model", lambda m: m or "auto")

    req = chat.ChatRequest(message="hi", mode="coding")

    # Consume the StreamingResponse
    response = await chat.send_message(req, member)
    async for _ in response.body_iterator:
        pass

    assert saved_args, "No assistant message was saved"
    assert saved_args[0].get("mode") == "coding", (
        f"Expected mode='coding' on assistant message, got: {saved_args[0]}"
    )
