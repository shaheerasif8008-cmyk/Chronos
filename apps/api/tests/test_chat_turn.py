import json
import pytest
from sqlalchemy import Column, MetaData, String, Table


def _ctx():
    from core.models import RequesterContext
    return RequesterContext(org_id="default", member_id="member-1", role="user")


async def _run(gen):
    return [ev async for ev in gen]


@pytest.mark.asyncio
async def test_no_tool_turn_streams_and_creates_no_task(monkeypatch):
    from runtime import agent_loop

    async def fake_stream_step(messages, tools, model):
        for t in ["Paris", " is", " the", " capital."]:
            yield {"type": "token", "content": t}
        yield {"type": "text_done", "text": "Paris is the capital."}

    created = []
    async def fake_create(**kwargs):
        created.append(kwargs)
        return "task-x"

    saved_msgs = []
    async def fake_save_assistant(conv_id, content, ctx, mode=None):
        saved_msgs.append(content)

    async def fake_extract(*a, **k):
        return None

    monkeypatch.setattr(agent_loop, "stream_step", fake_stream_step)
    monkeypatch.setattr(agent_loop, "create_task_from_history", fake_create)
    monkeypatch.setattr(agent_loop, "persist_assistant_message", fake_save_assistant)
    monkeypatch.setattr(agent_loop, "extract_and_save", fake_extract)

    events = await _run(agent_loop.stream_chat_turn(
        conversation_id="conv-1",
        message="capital of France?",
        context_messages=[{"role": "system", "content": "sys"}],
        requester_context=_ctx(),
        model="agent",
    ))

    tokens = "".join(e["content"] for e in events if e["type"] == "token")
    assert tokens == "Paris is the capital."
    assert created == []
    assert saved_msgs == ["Paris is the capital."]
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_first_tool_call_creates_task_with_full_history(monkeypatch):
    from runtime import agent_loop

    steps = [
        [{"type": "tool_calls", "calls": [{"id": "c1", "name": "browser__search", "args_str": "{}"}]}],
        [{"type": "token", "content": "Found it."}, {"type": "text_done", "text": "Found it."}],
    ]
    async def fake_stream_step(messages, tools, model):
        for ev in steps.pop(0):
            yield ev

    created = {}
    async def fake_create(*, history, **kwargs):
        created["history"] = history
        return "task-1"

    async def fake_execute(call, task, agent):
        return {"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                "content": json.dumps({"summary": "ok", "data": {}})}

    async def fake_get_task(tid):
        return {"id": tid, "organization_id": "default", "region": "us", "depth": 0,
                "triggered_by_member_id": "member-1", "workspace_id": None, "persona_id": None}

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(agent_loop, "stream_step", fake_stream_step)
    monkeypatch.setattr(agent_loop, "create_task_from_history", fake_create)
    monkeypatch.setattr(agent_loop, "_execute_tool", fake_execute)
    monkeypatch.setattr(agent_loop, "emit_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "save_task", noop)
    monkeypatch.setattr(agent_loop, "get_task", fake_get_task)
    monkeypatch.setattr(agent_loop, "persist_assistant_message", noop)
    monkeypatch.setattr(agent_loop, "extract_and_save", noop)

    events = await _run(agent_loop.stream_chat_turn(
        conversation_id="conv-1",
        message="news today",
        context_messages=[{"role": "system", "content": "sys"}],
        requester_context=_ctx(),
        model="agent",
    ))

    roles = [m["role"] for m in created["history"]]
    assert roles[0] == "system" and "user" in roles and "assistant" in roles
    assert any(e["type"] == "task_created" for e in events)
    assert "Found it." in "".join(e["content"] for e in events if e["type"] == "token")


@pytest.mark.asyncio
async def test_start_task_promotes_to_background(monkeypatch):
    from runtime import agent_loop

    async def fake_stream_step(messages, tools, model):
        yield {"type": "tool_calls", "calls": [{"id": "c1", "name": "start_task", "args_str": json.dumps({"goal": "research 50 leads"})}]}

    enqueued = []
    async def fake_create(*, history, goal, **kwargs):
        return "task-bg"
    async def fake_enqueue(task_id, priority=10):
        enqueued.append(task_id)
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(agent_loop, "stream_step", fake_stream_step)
    monkeypatch.setattr(agent_loop, "create_task_from_history", fake_create)
    monkeypatch.setattr(agent_loop.task_runner, "enqueue_task", fake_enqueue)
    monkeypatch.setattr(agent_loop, "emit_activity", noop)
    monkeypatch.setattr(agent_loop, "save_task", noop)
    monkeypatch.setattr(agent_loop, "extract_and_save", noop)

    events = await _run(agent_loop.stream_chat_turn(
        conversation_id="conv-1",
        message="research 50 AI law firms and draft outreach",
        context_messages=[{"role": "system", "content": "sys"}],
        requester_context=_ctx(),
        model="agent",
    ))

    assert enqueued == ["task-bg"]
    assert "gathering the relevant sources" in "".join(e.get("content", "") for e in events if e["type"] == "token")
    assert any(e["type"] == "task_created" and e.get("background") for e in events)
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_complex_inline_turn_sends_fast_ack_before_model_tokens(monkeypatch):
    from runtime import agent_loop

    async def fake_stream_step(messages, tools, model):
        yield {"type": "token", "content": "The"}
        yield {"type": "token", "content": " answer"}
        yield {"type": "text_done", "text": "The answer"}

    saved_msgs = []

    async def fake_save_assistant(conv_id, content, ctx, mode=None):
        saved_msgs.append(content)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(agent_loop, "stream_step", fake_stream_step)
    monkeypatch.setattr(agent_loop, "persist_assistant_message", fake_save_assistant)
    monkeypatch.setattr(agent_loop, "extract_and_save", noop)

    events = await _run(agent_loop.stream_chat_turn(
        conversation_id="conv-1",
        message="Analyze this contract and extract the liability terms compared to the prior draft.",
        context_messages=[{"role": "system", "content": "sys"}],
        requester_context=_ctx(),
        model="agent",
    ))

    token_events = [e["content"] for e in events if e["type"] == "token"]
    assert token_events[0].startswith("I'll start by checking the contract")
    assert "".join(token_events).endswith("The answer")
    assert saved_msgs == ["".join(token_events)]


@pytest.mark.asyncio
async def test_mailbox_turn_injects_gmail_grounding_before_user(monkeypatch):
    from runtime import agent_loop

    captured = {}
    steps = [
        [{"type": "tool_calls", "calls": [{"id": "c1", "name": "gmail__search", "args_str": json.dumps({"query": "newer_than:3d"})}]}],
        [{"type": "token", "content": "No matching emails found."}, {"type": "text_done", "text": "No matching emails found."}],
    ]

    async def fake_stream_step(messages, tools, model):
        captured.setdefault("messages", list(messages))
        for ev in steps.pop(0):
            yield ev

    async def fake_create(*, history, **kwargs):
        captured["created_history"] = history
        return "task-1"

    async def fake_get_task(tid):
        return {"id": tid, "organization_id": "default", "region": "us", "depth": 0,
                "triggered_by_member_id": "member-1", "workspace_id": None, "persona_id": None}

    async def fake_execute(call, task, agent):
        return {"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                "content": json.dumps({"summary": "Gmail search 'newer_than:3d': 0 result(s)", "data": {"result_count": 0, "threads": []}})}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(agent_loop, "stream_step", fake_stream_step)
    monkeypatch.setattr(agent_loop, "create_task_from_history", fake_create)
    monkeypatch.setattr(agent_loop, "get_task", fake_get_task)
    monkeypatch.setattr(agent_loop, "_execute_tool", fake_execute)
    monkeypatch.setattr(agent_loop, "emit_activity", noop)
    monkeypatch.setattr(agent_loop, "save_task", noop)
    monkeypatch.setattr(agent_loop, "persist_assistant_message", noop)
    monkeypatch.setattr(agent_loop, "extract_and_save", noop)

    await _run(agent_loop.stream_chat_turn(
        conversation_id="conv-1",
        message="summarise my emails in the last 3 days",
        context_messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "prior prompt"}],
        requester_context=_ctx(),
        model="agent",
    ))

    messages = captured["messages"]
    assert messages[-1] == {"role": "user", "content": "summarise my emails in the last 3 days"}
    assert messages[-2]["role"] == "system"
    assert "call gmail__search" in messages[-2]["content"]
    assert any(m["content"] == "prior prompt" for m in messages)
    assert captured["created_history"][: len(messages)] == messages


@pytest.mark.asyncio
async def test_task_history_uses_original_message_as_source_of_truth(monkeypatch):
    from runtime import agent_loop

    async def fake_system_message(tools=None):
        return {"role": "system", "content": "sys"}

    monkeypatch.setattr(agent_loop, "_agent_system_message", fake_system_message)

    history = await agent_loop._load_history({
        "id": "task-1",
        "goal": "Run the provided GitHub repository and analyze it.",
        "agent_state": {
            "agent_history": [],
            "original_user_message": (
                "https://github.com/shaheerasif8008-cmyk/Chronos.git "
                "this is my repo, run it and tell me about its strengths and weaknesses"
            ),
        },
    })

    assert history[-1]["role"] == "user"
    assert "Use the Original user request as the source of truth" in history[-1]["content"]
    assert "Original user request:" in history[-1]["content"]
    assert "https://github.com/shaheerasif8008-cmyk/Chronos.git" in history[-1]["content"]
    assert "Extracted entities:" in history[-1]["content"]
    assert "Router metadata:" in history[-1]["content"]


@pytest.mark.asyncio
async def test_task_envelope_beats_lossy_router_summary(monkeypatch):
    from runtime import agent_loop
    from core.task_envelope import build_task_envelope

    async def fake_system_message(tools=None):
        return {"role": "system", "content": "sys"}

    monkeypatch.setattr(agent_loop, "_agent_system_message", fake_system_message)

    raw = (
        "https://github.com/shaheerasif8008-cmyk/Chronos.git "
        "this is my repo, run it and tell me about its strengths and weaknesses"
    )
    envelope = build_task_envelope(
        task_id="task-1",
        raw_user_message=raw,
        ui_title="Run the provided GitHub repository and analyze its strengths and weaknesses",
        router_decision={
            "mode": "agent",
            "ui_title": "Run the provided GitHub repository and analyze its strengths and weaknesses",
        },
    )

    history = await agent_loop._load_history({
        "id": "task-1",
        "goal": "Run the provided GitHub repository and analyze its strengths and weaknesses",
        "agent_state": {"task_envelope": envelope.model_dump()},
    })

    prompt = history[-1]["content"]
    assert raw in prompt
    assert "https://github.com/shaheerasif8008-cmyk/Chronos.git" in prompt
    assert "Router metadata:" in prompt
    assert "Use the Original user request as the source of truth" in prompt


@pytest.mark.asyncio
async def test_task_stream_catch_up_includes_replay_events(monkeypatch):
    from core.models import Member
    from routers import tasks

    task_table = Table(
        "tasks",
        MetaData(),
        Column("id", String),
        Column("organization_id", String),
    )

    class FakeResult:
        def mappings(self):
            return self

        def first(self):
            return {"id": "task-1", "organization_id": "default", "goal": "research", "status": "running"}

    class FakeConn:
        async def execute(self, _stmt):
            return FakeResult()

    class FakeBegin:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, *_args):
            return None

    class FakeEngine:
        def begin(self):
            return FakeBegin()

    async def fake_reflect_table(name):
        assert name == "tasks"
        return task_table

    async def fake_check(*_args, **_kwargs):
        return True

    async def fake_events(task_id, org_id, limit=200, offset=0):
        return [{"type": "tool_call", "task_id": task_id, "summary": "Calling browser.search", "created_at": "2026-06-01T12:00:00Z"}]

    monkeypatch.setattr(tasks, "engine", FakeEngine())
    monkeypatch.setattr(tasks, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(tasks.permissions, "check", fake_check)
    monkeypatch.setattr(tasks, "list_task_events", fake_events)

    response = await tasks.stream_task(
        "task-1",
        member=Member(id="member-1", email="m@example.com", organization_id="default"),
    )
    first = await response.body_iterator.__anext__()
    payload = json.loads(first.removeprefix("data: ").strip())

    assert payload["type"] == "catch_up"
    assert payload["task"]["id"] == "task-1"
    assert payload["events"][0]["summary"] == "Calling browser.search"


@pytest.mark.asyncio
async def test_inline_turn_gates_external_write_after_prompt_injection(monkeypatch):
    from runtime import agent_loop

    steps = [
        [{"type": "tool_calls", "calls": [{"id": "f1", "name": "browser__fetch", "args_str": "{}"}]}],
        [{"type": "tool_calls", "calls": [{"id": "d1", "name": "gmail__draft", "args_str": "{}"}]}],
    ]

    async def fake_stream_step(messages, tools, model):
        for ev in steps.pop(0):
            yield ev

    async def fake_create(**k):
        return "task-inj"

    async def fake_get_task(tid):
        return {"id": tid, "organization_id": "default", "region": "us", "depth": 0,
                "triggered_by_member_id": "member-1", "workspace_id": None, "persona_id": None}

    async def fake_execute(call, task, agent):
        if call["name"] != "browser__fetch":
            raise AssertionError("external write must not execute after injection without approval")
        return {"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                "content": json.dumps({"summary": "fetched", "data": {"untrusted_content": {
                    "trusted": False, "risk": "prompt_injection", "source": "browser:https://x"}}})}

    gated = []
    async def fake_open_gate(task, pending, history, iteration, model=None):
        gated.extend(c["name"] for c in pending)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(agent_loop, "stream_step", fake_stream_step)
    monkeypatch.setattr(agent_loop, "create_task_from_history", fake_create)
    monkeypatch.setattr(agent_loop, "get_task", fake_get_task)
    monkeypatch.setattr(agent_loop, "_execute_tool", fake_execute)
    monkeypatch.setattr(agent_loop, "_open_approval_gate", fake_open_gate)
    monkeypatch.setattr(agent_loop, "emit_activity", noop)
    monkeypatch.setattr(agent_loop, "save_task", noop)
    monkeypatch.setattr(agent_loop, "_checkpoint", noop)
    monkeypatch.setattr(agent_loop, "extract_and_save", noop)

    events = await _run(agent_loop.stream_chat_turn(
        conversation_id="conv-1",
        message="fetch the page then email a summary",
        context_messages=[{"role": "system", "content": "sys"}],
        requester_context=_ctx(),
        model="agent",
    ))

    assert gated == ["gmail__draft"]
    assert any(e["type"] == "awaiting_approval" for e in events)
