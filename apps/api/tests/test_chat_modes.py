"""Composer mode selector — TDD tests.

Run failing-first (before implementation), then pass after changes.

Tests:
1. normalize_mode (from core.modes) coerces unknown/None → "default", passes valid modes through,
   lowercases/trims before checking.
2. create_task_record writes (normalized) mode to the task insert row.
3. Fast-path send_message persists chosen mode on the assistant message.
"""

import pytest
from unittest.mock import AsyncMock


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


@pytest.mark.parametrize(
    "mode",
    [
        "default",
        "chat",
        "research",
        "agent",
        "browser",
        "computer",
        "data",
        "image",
        "voice",
        "coding",
    ],
)
def test_normalize_mode_valid_passes_through(mode):
    from core.modes import normalize_mode

    assert normalize_mode(mode) == mode


def test_normalize_mode_lowercases():
    from core.modes import normalize_mode

    assert normalize_mode("Coding") == "coding"


def test_normalize_mode_trims_whitespace():
    from core.modes import normalize_mode

    assert normalize_mode(" agent ") == "agent"


def test_available_modes_expose_only_default():
    """Chronos is chat-first: only the default self-routing mode is advertised.

    The historical modes remain valid inputs to normalize_mode (backward compat),
    but they are no longer surfaced as a user-facing composer choice.
    """
    from core.modes import available_modes

    modes = available_modes()

    assert [mode["id"] for mode in modes] == ["default"]
    only = modes[0]
    assert only["status"] in {"available", "foundation", "unavailable"}
    assert isinstance(only["capabilities"], list)
    assert only["label"]
    assert only["description"]
    assert "creates_task" in only


def test_model_kwargs_threads_reasoning_effort_for_supported_providers():
    from core.llm import model_kwargs

    kwargs = model_kwargs(
        "openai/gpt-5.4-mini",
        messages=[{"role": "user", "content": "hi"}],
        reasoning_effort="High",
    )

    assert kwargs["reasoning_effort"] == "high"


def test_model_kwargs_threads_reasoning_effort_for_routed_reasoning_models():
    # Reasoning models keep their effort even when routed through OpenRouter —
    # the gate is on the model family, not the provider prefix. (Previously this
    # was dropped for all openrouter/ models, silently disabling reasoning.)
    from core.llm import model_kwargs

    kwargs = model_kwargs(
        "openrouter/openai/gpt-5.4-mini",
        messages=[{"role": "user", "content": "hi"}],
        reasoning_effort="High",
    )

    assert kwargs["reasoning_effort"] == "high"


def test_model_kwargs_drops_reasoning_effort_for_non_reasoning_models():
    from core.llm import model_kwargs

    kwargs = model_kwargs(
        "openrouter/meta-llama/llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        reasoning_effort="High",
    )

    assert "reasoning_effort" not in kwargs


def test_model_accepts_reasoning_effort_gates_on_family_not_provider():
    from core.llm import model_accepts_reasoning_effort

    # Reasoning families qualify regardless of the provider prefix.
    assert model_accepts_reasoning_effort("openrouter/openai/gpt-5.4-mini") is True
    assert model_accepts_reasoning_effort("openai/gpt-5.4-nano") is True
    assert model_accepts_reasoning_effort("openrouter/deepseek/deepseek-v4-pro") is True
    # Non-reasoning models do not.
    assert (
        model_accepts_reasoning_effort("openrouter/meta-llama/llama-3-8b-instruct")
        is False
    )
    assert (
        model_accepts_reasoning_effort("openrouter/deepseek/deepseek-v4-flash") is False
    )
    assert model_accepts_reasoning_effort(None) is False


def test_model_kwargs_rejects_unknown_reasoning_effort():
    from core.llm import model_kwargs

    with pytest.raises(ValueError, match="unknown reasoning effort"):
        model_kwargs(
            "openrouter/openai/gpt-5.4-mini",
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="maximum",
        )


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
        reasoning_effort="high",
    )

    assert task_id == "task-uuid-123"
    assert insert_values.get("mode") == "research", (
        f"mode missing from task insert; got: {insert_values}"
    )
    assert insert_values["agent_state"]["reasoning_effort"] == "high"
    envelope = insert_values["agent_state"]["task_envelope"]
    assert envelope["raw_user_message"] == "Test goal"
    assert envelope["ui"]["title"] == "Test goal"


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
async def test_send_message_threads_mode_into_inline_turn(monkeypatch):
    """send_message threads the request mode into the inline streaming turn.

    The turn persists it onto the assistant message (agent_loop.persist_assistant_message),
    so the request's mode — not a hardcoded literal — ends up on the saved row.
    """
    from routers import chat
    from core.models import Member

    member = Member(
        id="member-1",
        organization_id="default",
        email="admin@example.com",
        role="admin",
    )

    captured: dict = {}

    async def fake_stream_chat_turn(**kwargs):
        captured["mode"] = kwargs.get("mode")
        captured["reasoning_effort"] = kwargs.get("reasoning_effort")
        yield {"type": "done"}

    async def fake_permissions_check(*args, **kwargs):
        return True

    async def fake_create_conversation(member, title, **kwargs):
        return "conv-new"

    async def fake_save_message(*args, **kwargs):
        return None

    async def fake_assemble_context(conv_id, msg, ctx):
        return [{"role": "user", "content": msg}]

    monkeypatch.setattr(chat.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(
        chat.workspace_access,
        "require_workspace_access",
        AsyncMock(return_value={"id": "workspace-1", "status": "active"}),
    )
    monkeypatch.setattr(chat, "_create_conversation", fake_create_conversation)
    monkeypatch.setattr(chat, "_save_message", fake_save_message)
    monkeypatch.setattr(chat, "assemble_context", fake_assemble_context)
    monkeypatch.setattr(chat, "extract_explicit_memory_content", lambda msg: None)
    monkeypatch.setattr(chat, "stream_chat_turn", fake_stream_chat_turn)

    req = chat.ChatRequest(
        message="hi", model="gpt-5.4-mini", mode="coding", reasoning_effort="high"
    )

    response = await chat.send_message(req, member)
    async for _ in response.body_iterator:
        pass

    assert captured.get("mode") == "coding", (
        f"Expected mode='coding' threaded into the inline turn, got: {captured}"
    )
    assert captured.get("reasoning_effort") == "high"


@pytest.mark.asyncio
async def test_nontrivial_chat_uses_history_aware_inline_turn(monkeypatch):
    """Non-trivial chat should not create a bare background task with empty history."""
    from routers import chat
    from core.models import Member

    member = Member(
        id="member-1",
        organization_id="default",
        email="admin@example.com",
        role="admin",
    )

    captured: dict = {}

    async def fake_stream_chat_turn(**kwargs):
        captured["context_messages"] = kwargs.get("context_messages")
        captured["message"] = kwargs.get("message")
        yield {"type": "done"}

    async def fake_agent_loop_stream(**kwargs):
        raise AssertionError("ordinary chat should not use the bare task stream")
        yield ""  # pragma: no cover

    async def fake_permissions_check(*args, **kwargs):
        return True

    async def fake_create_conversation(member, title, **kwargs):
        return "conv-new"

    async def fake_save_message(*args, **kwargs):
        return None

    async def fake_assemble_context(conv_id, msg, ctx):
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "summarise my emails in the last 3 days"},
            {"role": "assistant", "content": "No matching emails found."},
            {"role": "user", "content": msg},
        ]

    monkeypatch.setattr(chat.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(
        chat.workspace_access,
        "require_workspace_access",
        AsyncMock(return_value={"id": "workspace-1", "status": "active"}),
    )
    monkeypatch.setattr(chat, "_create_conversation", fake_create_conversation)
    monkeypatch.setattr(chat, "_save_message", fake_save_message)
    monkeypatch.setattr(chat, "assemble_context", fake_assemble_context)
    monkeypatch.setattr(chat, "extract_explicit_memory_content", lambda msg: None)
    monkeypatch.setattr(
        chat,
        "classify_request",
        AsyncMock(
            return_value={
                "mode": "chat",
                "goal": None,
                "difficulty": "standard",
                "reasoning_effort": "medium",
            }
        ),
    )
    monkeypatch.setattr(chat, "stream_chat_turn", fake_stream_chat_turn)
    monkeypatch.setattr(chat, "_agent_loop_stream", fake_agent_loop_stream)

    req = chat.ChatRequest(message="what email did you search", model="gpt-5.4-mini")

    response = await chat.send_message(req, member)
    async for _ in response.body_iterator:
        pass

    assert captured["message"] == "what email did you search"
    assert any(
        m["content"] == "summarise my emails in the last 3 days"
        for m in captured["context_messages"]
    )


@pytest.mark.asyncio
async def test_explicit_chat_mode_skips_durable_task_classifier(monkeypatch):
    from core.models import Member
    from routers import chat

    member = Member(
        id="member-1",
        organization_id="default",
        email="admin@example.com",
        role="admin",
    )
    classifier = AsyncMock(
        side_effect=AssertionError("explicit chat mode must not invoke task routing")
    )
    captured: dict = {}

    async def fake_stream_chat_turn(**kwargs):
        captured.update(kwargs)
        yield {"type": "done"}

    async def fake_permissions_check(*_args, **_kwargs):
        return True

    async def fake_create_conversation(*_args, **_kwargs):
        return "conv-direct"

    async def fake_save_message(*_args, **_kwargs):
        return None

    async def fake_assemble_context(_conversation_id, message, _context):
        return [{"role": "user", "content": message}]

    monkeypatch.setattr(chat.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(
        chat.workspace_access,
        "require_workspace_access",
        AsyncMock(return_value={"id": "workspace-1", "status": "active"}),
    )
    monkeypatch.setattr(chat, "_create_conversation", fake_create_conversation)
    monkeypatch.setattr(chat, "_save_message", fake_save_message)
    monkeypatch.setattr(chat, "assemble_context", fake_assemble_context)
    monkeypatch.setattr(chat, "extract_explicit_memory_content", lambda _msg: None)
    monkeypatch.setattr(chat, "classify_request", classifier)
    monkeypatch.setattr(chat, "stream_chat_turn", fake_stream_chat_turn)

    response = await chat.send_message(
        chat.ChatRequest(
            message="Analyze the supplied evidence without external actions.",
            model="gpt-5.4-mini",
            mode="chat",
        ),
        member,
    )
    async for _ in response.body_iterator:
        pass

    classifier.assert_not_awaited()
    assert captured["mode"] == "chat"


@pytest.mark.asyncio
async def test_task_route_preserves_original_message_for_execution(monkeypatch):
    from routers import chat
    from core.models import Member

    member = Member(
        id="member-1",
        organization_id="default",
        email="admin@example.com",
        role="admin",
    )
    captured: dict = {}

    async def fake_agent_loop_stream(**kwargs):
        captured.update(kwargs)
        yield 'data: {"type":"done"}\n\n'

    async def fake_permissions_check(*args, **kwargs):
        return True

    async def fake_create_conversation(member, title, **kwargs):
        return "conv-new"

    async def fake_save_message(*args, **kwargs):
        return None

    async def fake_assemble_context(conv_id, msg, ctx):
        return [{"role": "system", "content": "sys"}, {"role": "user", "content": msg}]

    original = (
        "https://github.com/shaheerasif8008-cmyk/Chronos.git "
        "this is my repo, run it and tell me about its strengths and weaknesses"
    )

    monkeypatch.setattr(chat.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(
        chat.workspace_access,
        "require_workspace_access",
        AsyncMock(return_value={"id": "workspace-1", "status": "active"}),
    )
    monkeypatch.setattr(chat, "_create_conversation", fake_create_conversation)
    monkeypatch.setattr(chat, "_save_message", fake_save_message)
    monkeypatch.setattr(chat, "assemble_context", fake_assemble_context)
    monkeypatch.setattr(chat, "extract_explicit_memory_content", lambda msg: None)
    monkeypatch.setattr(
        chat,
        "classify_request",
        AsyncMock(
            return_value={
                "mode": "task",
                "goal": "Run the provided GitHub repository and analyze its strengths and weaknesses",
                "difficulty": "hard",
                "reasoning_effort": "high",
            }
        ),
    )
    monkeypatch.setattr(chat, "_agent_loop_stream", fake_agent_loop_stream)

    response = await chat.send_message(
        chat.ChatRequest(message=original, model="gpt-5.4-mini"), member
    )
    async for _ in response.body_iterator:
        pass

    assert (
        captured["goal"]
        == "Run the provided GitHub repository and analyze its strengths and weaknesses"
    )
    assert captured["original_message"] == original
    assert captured["conversation_context"] == [{"role": "system", "content": "sys"}]
    assert (
        captured["router_decision"]["ui_title"]
        == "Run the provided GitHub repository and analyze its strengths and weaknesses"
    )
    assert (
        captured["router_decision"]["metadata"]["classifier"]["goal"]
        == "Run the provided GitHub repository and analyze its strengths and weaknesses"
    )
