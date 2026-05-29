# Streaming Chat Collapse — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse Chronos so every chat message runs the existing native agent loop inline and streams, with the heavy task/planner/classifier machinery deleted or made opt-in, so chat is fast and the durable task tier survives only for genuinely long work.

**Architecture:** One streaming path. The chat router streams a native agent turn inline (real model tokens). Quick tool use happens in the request; the `tasks` row is created lazily on the first tool call (persisting the full in-flight history). When the model calls a new `start_task` tool, the work is enqueued to the existing background worker (the durability backbone) and the chat attaches to its Redis activity stream. The DAG planner/executor, the `intent` classifier, the `route_tool` pre-call, and the per-step `reasoning_summary` LLM call are deleted.

**Tech Stack:** Python 3.11, FastAPI (SSE `StreamingResponse`), litellm (OpenRouter / `deepseek-v4-pro`), SQLAlchemy Core, Redis pub/sub, pytest + `pytest.mark.asyncio`, `monkeypatch.setattr` mocking.

**Spec:** `docs/superpowers/specs/2026-05-29-streaming-chat-collapse-design.md`

**Working directory for all commands:** `apps/api/`

---

## File map

| File | Change |
|------|--------|
| `apps/api/scripts/spike_streaming_tools.py` | Create (throwaway spike, Task 1) |
| `apps/api/core/llm.py` | Add `stream_step()` + streamed-tool-call accumulation helpers |
| `apps/api/runtime/tool_registry.py` | Add `START_TASK` schema + `INLINE_CHAT_TOOLS` set |
| `apps/api/runtime/agent_loop.py` | Add `stream_chat_turn()`; remove `route_tool` + `publish_reasoning_summary`; revise system prompt |
| `apps/api/routers/chat.py` | Rewire `send_message` to stream inline; delete routing gate |
| `apps/api/routers/tasks.py` | Reuse `create_task_record`; add a "create from history" variant |
| `apps/api/runtime/executor.py` | Delete DAG path; reduce `TaskExecutor` to native-loop shim |
| `apps/api/runtime/planner.py` | Delete file |
| `apps/api/core/tool_router.py` | Delete file |
| `apps/api/core/intent.py` | Delete file |
| `apps/api/tests/*` | Add new-behavior tests; delete obsolete tests (self-verifying) |

**Do NOT touch** `apps/api/connectors/framework/planner.py` — that is the connector-framework planner, unrelated to the DAG task planner.

---

## Phase 0 — De-risk

### Task 1: Spike streaming-with-tools against the real model

**Files:**
- Create: `apps/api/scripts/spike_streaming_tools.py`

- [ ] **Step 1: Write the spike**

```python
"""Throwaway spike: confirm litellm streaming yields content deltas for an
answer turn and accumulable tool_call deltas for an action turn on the
configured agent model. Delete after recording the result in the plan."""
import asyncio
import litellm
from core.config import settings
from runtime.tool_registry import ALL_TOOLS


async def probe(label: str, message: str) -> None:
    print(f"\n=== {label}: {message!r} ===")
    kwargs = {
        "model": settings.agent_model,
        "api_key": settings.openrouter_api_key,
        "api_base": settings.openrouter_api_base,
        "messages": [{"role": "user", "content": message}],
        "tools": ALL_TOOLS,
        "tool_choice": "auto",
        "stream": True,
    }
    stream = await litellm.acompletion(**kwargs)
    content_chunks = 0
    toolcall_chunks = 0
    async for chunk in stream:
        delta = chunk.choices[0].delta
        if getattr(delta, "content", None):
            content_chunks += 1
        if getattr(delta, "tool_calls", None):
            toolcall_chunks += 1
            print("  tool_call delta:", delta.tool_calls)
    print(f"  content_chunks={content_chunks} toolcall_chunks={toolcall_chunks}")


async def main() -> None:
    await probe("answer turn", "Explain in two sentences how OAuth works.")
    await probe("action turn", "What is the latest news today? Search the web.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run it**

Run: `python -m scripts.spike_streaming_tools`
Expected: answer turn → `content_chunks > 0, toolcall_chunks == 0`; action turn → `toolcall_chunks > 0`, and the printed tool_call deltas carry `index`, `id`, `function.name`, `function.arguments` fragments.

- [ ] **Step 3: Record the result + decision in this plan**

Edit this file, under this task, add:
`SPIKE RESULT: <paste the two summary lines>. Streamed tool-call deltas usable: YES/NO.`
- If **YES** → Task 2 streams tool-decision turns.
- If **NO** (deltas missing `id`/name or arguments not assemblable) → Task 2's `stream_step` finishes tool-decision turns with a **non-streaming** `_llm_step` call behind a "Searching…" trace. Either way the answer (content) turn streams — that is the user-visible win.

- [ ] **Step 4: Delete the spike, commit the decision**

```bash
git rm apps/api/scripts/spike_streaming_tools.py
git add docs/superpowers/plans/2026-05-29-streaming-chat-collapse.md
git commit -m "chore: record streaming-with-tools spike result"
```

---

## Phase 1 — Streaming foundation (additive, nothing breaks)

### Task 2: `stream_step` in `core/llm.py`

**Files:**
- Modify: `apps/api/core/llm.py`
- Test: `apps/api/tests/test_streaming_step.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_streaming_step.py
import pytest


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Chunk:
    def __init__(self, delta):
        self.choices = [_Choice(delta)]


class _ToolCallFrag:
    def __init__(self, index, id=None, name=None, args=""):
        self.index = index
        self.id = id
        self.function = type("F", (), {"name": name, "arguments": args})()


async def _aiter(chunks):
    for c in chunks:
        yield c


@pytest.mark.asyncio
async def test_stream_step_relays_content_tokens(monkeypatch):
    from core import llm

    async def fake_acompletion(**kwargs):
        assert kwargs["stream"] is True
        return _aiter([_Chunk(_Delta(content="Hel")), _Chunk(_Delta(content="lo"))])

    monkeypatch.setattr(llm.litellm, "acompletion", fake_acompletion)

    tokens, events = [], []
    async for ev in llm.stream_step([{"role": "user", "content": "hi"}], [], "m"):
        events.append(ev)
        if ev["type"] == "token":
            tokens.append(ev["content"])

    assert "".join(tokens) == "Hello"
    assert events[-1] == {"type": "text_done", "text": "Hello"}


@pytest.mark.asyncio
async def test_stream_step_accumulates_tool_calls(monkeypatch):
    from core import llm

    async def fake_acompletion(**kwargs):
        return _aiter([
            _Chunk(_Delta(tool_calls=[_ToolCallFrag(0, id="c1", name="browser__search", args='{"q')])),
            _Chunk(_Delta(tool_calls=[_ToolCallFrag(0, args='uery":"x"}')])),
        ])

    monkeypatch.setattr(llm.litellm, "acompletion", fake_acompletion)

    events = [ev async for ev in llm.stream_step([{"role": "user", "content": "find x"}], [], "m")]

    assert not [e for e in events if e["type"] == "token"]
    final = events[-1]
    assert final["type"] == "tool_calls"
    assert final["calls"] == [{"id": "c1", "name": "browser__search", "args_str": '{"query":"x"}'}]
```

- [ ] **Step 2: Run it (fails: no `stream_step`)**

Run: `pytest tests/test_streaming_step.py -v`
Expected: FAIL — `AttributeError: module 'core.llm' has no attribute 'stream_step'`

- [ ] **Step 3: Implement `stream_step` + delta accessors in `core/llm.py`**

Add near the other accessor helpers (after `_tool_calls`):

```python
def _delta(chunk: Any) -> Any:
    if isinstance(chunk, dict):
        return chunk.get("choices", [{}])[0].get("delta", {})
    return chunk.choices[0].delta


def _delta_content(delta: Any) -> str:
    if isinstance(delta, dict):
        return delta.get("content") or ""
    return getattr(delta, "content", None) or ""


def _delta_tool_calls(delta: Any) -> list[Any]:
    if isinstance(delta, dict):
        return delta.get("tool_calls") or []
    return getattr(delta, "tool_calls", None) or []


def _frag_fields(frag: Any) -> tuple[int, str | None, str | None, str]:
    """Return (index, id, name, args_fragment) from a streamed tool_call delta."""
    if isinstance(frag, dict):
        fn = frag.get("function") or {}
        return int(frag.get("index") or 0), frag.get("id"), fn.get("name"), fn.get("arguments") or ""
    fn = getattr(frag, "function", None)
    return (
        int(getattr(frag, "index", 0) or 0),
        getattr(frag, "id", None),
        getattr(fn, "name", None),
        getattr(fn, "arguments", "") or "",
    )
```

Then the generator:

```python
async def stream_step(messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str):
    """Stream one model step.

    Yields `{"type": "token", "content": str}` for answer turns, then a terminal
    `{"type": "text_done", "text": str}`. For action turns yields no tokens and a
    terminal `{"type": "tool_calls", "calls": [{"id","name","args_str"}, ...]}`.
    """
    kwargs = model_kwargs(model, messages=messages, stream=True)
    kwargs["tools"] = tools
    kwargs["tool_choice"] = "auto"
    stream = await _with_retry(lambda: litellm.acompletion(**kwargs))

    text_parts: list[str] = []
    # index -> {"id", "name", "args"}
    acc: dict[int, dict[str, Any]] = {}
    async for chunk in stream:
        delta = _delta(chunk)
        content = _delta_content(delta)
        if content:
            text_parts.append(content)
            yield {"type": "token", "content": content}
        for frag in _delta_tool_calls(delta):
            idx, fid, name, args = _frag_fields(frag)
            slot = acc.setdefault(idx, {"id": None, "name": None, "args": ""})
            if fid:
                slot["id"] = fid
            if name:
                slot["name"] = name
            slot["args"] += args

    if acc:
        calls = [
            {"id": s["id"] or f"call_{i}", "name": s["name"] or "", "args_str": s["args"] or "{}"}
            for i, s in sorted(acc.items())
        ]
        yield {"type": "tool_calls", "calls": calls}
    else:
        yield {"type": "text_done", "text": "".join(text_parts)}
```

> If Task 1 recorded **NO**, replace the `async for chunk` body's tool handling: detect any `_delta_tool_calls` non-empty on the first chunk, abandon the stream, and fall back to `tool_call(messages, tools, model=model)` (non-streaming, already in `llm.py`) — emit its calls as the terminal `tool_calls` event. Keep the content-streaming path unchanged.

- [ ] **Step 4: Run it (passes)**

Run: `pytest tests/test_streaming_step.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add core/llm.py tests/test_streaming_step.py
git commit -m "feat(llm): add stream_step with streamed tool-call accumulation"
```

---

### Task 3: `start_task` tool + inline tool set

**Files:**
- Modify: `apps/api/runtime/tool_registry.py`
- Test: `apps/api/tests/test_inline_tools.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_inline_tools.py
def test_inline_tools_include_start_task_and_exclude_spawn():
    from runtime.tool_registry import INLINE_CHAT_TOOLS, tool_name

    names = {tool_name(t) for t in INLINE_CHAT_TOOLS}
    assert "start_task" in names
    assert "spawn__subagent" not in names
    assert "browser__search" in names


def test_durable_tools_include_spawn_and_exclude_start_task():
    from runtime.tool_registry import ALL_TOOLS, tool_name

    names = {tool_name(t) for t in ALL_TOOLS}
    assert "spawn__subagent" in names
    assert "start_task" not in names
```

- [ ] **Step 2: Run it (fails)**

Run: `pytest tests/test_inline_tools.py -v`
Expected: FAIL — `ImportError: cannot import name 'INLINE_CHAT_TOOLS'`

- [ ] **Step 3: Implement in `tool_registry.py`**

After the `SPAWN_SUBAGENT` definition add:

```python
START_TASK = _fn(
    "start_task",
    "Promote the current request into a durable background task. Call this ONLY "
    "when the work is large and long-running (multi-step research, batch outreach, "
    "anything that will take minutes or spawn sub-agents). The task runs in the "
    "background, survives disconnects, streams its activity, and routes risky "
    "actions through approvals. For quick questions or a single lookup, just "
    "answer or use a tool directly — do NOT call start_task.",
    {
        "goal": {"type": "string", "description": "Clear, self-contained goal for the durable task."},
    },
    ["goal"],
)
```

After the existing `ALL_TOOLS` / `SUBAGENT_TOOLS` definitions add:

```python
#: Tools available to an inline chat turn: quick tools + promotion. No recursive
#: sub-agent spawning inline — large work promotes via start_task instead.
INLINE_CHAT_TOOLS: list[dict[str, Any]] = [
    BROWSER_SEARCH,
    BROWSER_FETCH,
    BROWSER_EXTRACT_CONTACTS,
    GMAIL_DRAFT,
    GMAIL_SEARCH,
    FS_LIST,
    FS_READ,
    FS_WRITE,
    CODE_PYTHON,
    DOC_PARSE,
    DOC_READ,
    START_TASK,
]

_START_TASK_TOOL_NAME = "start_task"
```

Leave `ALL_TOOLS` as-is (it already excludes `start_task` and includes `spawn__subagent`).

- [ ] **Step 4: Run it (passes)**

Run: `pytest tests/test_inline_tools.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime/tool_registry.py tests/test_inline_tools.py
git commit -m "feat(tools): add start_task promotion tool and INLINE_CHAT_TOOLS set"
```

---

## Phase 2 — Inline streaming turn

### Task 4: `stream_chat_turn` in `agent_loop.py`

This is the core new code. It assembles context, streams the model, relays tokens,
lazily creates the task row **with full history** on first tool call, executes
quick tools inline, opens the approval gate when the broker gates, and promotes to
a durable task when the model calls `start_task`.

**Files:**
- Modify: `apps/api/runtime/agent_loop.py`
- Test: `apps/api/tests/test_chat_turn.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# apps/api/tests/test_chat_turn.py
import json
import pytest


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
    async def fake_save_assistant(conv_id, content, ctx):
        saved_msgs.append(content)

    monkeypatch.setattr(agent_loop, "stream_step", fake_stream_step)
    monkeypatch.setattr(agent_loop, "create_task_from_history", fake_create)
    monkeypatch.setattr(agent_loop, "persist_assistant_message", fake_save_assistant)
    monkeypatch.setattr(agent_loop, "extract_and_save", lambda *a, **k: None)

    events = await _run(agent_loop.stream_chat_turn(
        conversation_id="conv-1",
        message="capital of France?",
        context_messages=[{"role": "system", "content": "sys"}],
        requester_context=_ctx(),
        model="agent",
    ))

    tokens = "".join(e["content"] for e in events if e["type"] == "token")
    assert tokens == "Paris is the capital."
    assert created == []                      # no tool → no task row
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

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(agent_loop, "stream_step", fake_stream_step)
    monkeypatch.setattr(agent_loop, "create_task_from_history", fake_create)
    monkeypatch.setattr(agent_loop, "_execute_tool", fake_execute)
    monkeypatch.setattr(agent_loop, "emit_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "save_task", noop)
    monkeypatch.setattr(agent_loop, "get_task", lambda tid: {"id": tid, "organization_id": "default", "region": "us", "depth": 0})
    monkeypatch.setattr(agent_loop, "persist_assistant_message", noop)
    monkeypatch.setattr(agent_loop, "extract_and_save", lambda *a, **k: None)

    events = await _run(agent_loop.stream_chat_turn(
        conversation_id="conv-1",
        message="news today",
        context_messages=[{"role": "system", "content": "sys"}],
        requester_context=_ctx(),
        model="agent",
    ))

    # Full in-flight history persisted (system + user + assistant tool-call msg), not a bare goal.
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
    monkeypatch.setattr(agent_loop, "extract_and_save", lambda *a, **k: None)

    events = await _run(agent_loop.stream_chat_turn(
        conversation_id="conv-1",
        message="research 50 AI law firms and draft outreach",
        context_messages=[{"role": "system", "content": "sys"}],
        requester_context=_ctx(),
        model="agent",
    ))

    assert enqueued == ["task-bg"]
    assert any(e["type"] == "task_created" and e.get("background") for e in events)
    assert events[-1]["type"] == "done"
```

- [ ] **Step 2: Run it (fails)**

Run: `pytest tests/test_chat_turn.py -v`
Expected: FAIL — `AttributeError: module 'runtime.agent_loop' has no attribute 'stream_chat_turn'`

- [ ] **Step 3: Implement in `agent_loop.py`**

Add imports at top:

```python
from runtime import task_runner
from runtime.tool_registry import INLINE_CHAT_TOOLS, _START_TASK_TOOL_NAME
from memory.extraction import extract_and_save
```

Add a thin persistence wrapper (reuses the existing `_save_assistant_message`):

```python
async def persist_assistant_message(conversation_id: str, content: str, requester_context: Any) -> None:
    """Save an inline chat turn's final answer as an assistant message. Never raises."""
    try:
        await _save_assistant_message(
            conversation_id,
            content,
            {"id": None, "organization_id": requester_context.org_id, "region": "us"},
        )
    except Exception as exc:  # persistence must never break the turn
        logger.warning("Failed to persist inline assistant message: %s", exc)
```

Add the lazy task creator (delegates to the router helper, persisting full history):

```python
async def create_task_from_history(
    *,
    goal: str,
    history: list[dict[str, Any]],
    requester_context: Any,
    conversation_id: str,
    model: str | None,
    status: str = "running",
) -> str:
    """Create a tasks row seeded with the full in-flight history (not a bare goal).

    Used both for lazy persistence on the first inline tool call and for start_task
    promotion. The stored agent_history is what resume/approval rebuilds from.
    """
    from core.llm import resolve_agent_model

    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(tasks)
            .values(
                organization_id=requester_context.org_id,
                region="us",
                persona_id=requester_context.persona_id,
                workspace_id=requester_context.workspace_id,
                triggered_by=conversation_id,
                triggered_by_member_id=requester_context.member_id,
                status=status,
                goal=goal,
                plan={},
                agent_state={
                    "agent_history": history,
                    "iteration_count": 0,
                    "model": resolve_agent_model(model),
                },
                current_step=0,
                result={},
                depth=0,
            )
            .returning(tasks.c.id)
        )
        return str(result.scalar_one())
```

Add the streaming turn generator:

```python
async def stream_chat_turn(
    *,
    conversation_id: str,
    message: str,
    context_messages: list[dict[str, Any]],
    requester_context: Any,
    model: str | None,
):
    """Stream one chat turn inline.

    Yields SSE-ready event dicts: token / trace / artifact / task_created /
    awaiting_approval / error / done. Creates a tasks row lazily on the first
    tool call (with full history). Promotes to a durable background task when the
    model calls start_task.
    """
    yield {"type": "conversation", "conversation_id": conversation_id}

    history: list[dict[str, Any]] = list(context_messages) + [{"role": "user", "content": message}]
    effective_model = resolve_agent_model(model)
    task_id: str | None = None          # lazily created
    task: dict[str, Any] | None = None
    iteration = 0

    while iteration < MAX_ITERATIONS:
        iteration += 1
        final_text: str | None = None
        calls: list[dict[str, Any]] = []
        try:
            async for ev in stream_step(history, INLINE_CHAT_TOOLS, effective_model):
                if ev["type"] == "token":
                    yield {"type": "token", "content": ev["content"]}
                elif ev["type"] == "text_done":
                    final_text = ev["text"]
                elif ev["type"] == "tool_calls":
                    calls = ev["calls"]
        except Exception as exc:
            logger.error("Inline turn model error: %s", exc)
            msg = "Sorry — I hit a model error and couldn't finish that. Please try again."
            await persist_assistant_message(conversation_id, msg, requester_context)
            yield {"type": "token", "content": msg}
            yield {"type": "done"}
            return

        # ── Answer turn: streamed already, persist + extract + finish ──
        if not calls:
            answer = final_text or ""
            await persist_assistant_message(conversation_id, answer, requester_context)
            asyncio.create_task(extract_and_save(conversation_id, message, answer, requester_context))
            yield {"type": "done"}
            return

        # ── Promotion: start_task → enqueue durable background task ──
        promote = next((c for c in calls if c["name"] == _START_TASK_TOOL_NAME), None)
        if promote:
            goal = _parse_args(promote["args_str"]).get("goal") or message
            bg_id = await create_task_from_history(
                goal=goal, history=history, requester_context=requester_context,
                conversation_id=conversation_id, model=model, status="queued",
            )
            await task_runner.enqueue_task(bg_id)
            yield {"type": "task_created", "task_id": bg_id, "background": True}
            yield {"type": "done"}
            return

        # ── Action turn: lazily create the task row with FULL history ──
        history.append(_serialise_assistant(calls, final_text))
        if task_id is None:
            task_id = await create_task_from_history(
                goal=message, history=history, requester_context=requester_context,
                conversation_id=conversation_id, model=model, status="running",
            )
            task = await get_task(task_id)
            yield {"type": "task_created", "task_id": task_id, "background": False}

        agent = AgentContext.from_task(task)

        # Approval-gated tools pause the turn and hand off to the durable resume path.
        approval_needed = [c for c in calls if _needs_approval(c["name"])]
        if approval_needed:
            await _open_approval_gate(task, approval_needed, history, iteration, model=effective_model)
            yield {"type": "awaiting_approval", "task_id": task_id}
            yield {"type": "done"}
            return

        for call in calls:
            yield {"type": "trace", "event": {"type": "tool_call", "tool": call["name"], "args_preview": _args_preview(_parse_args(call["args_str"]))}}
            try:
                tool_msg = await _execute_tool(call, task, agent)
            except ApprovalRequired:
                await _open_approval_gate(task, [call], history, iteration, model=effective_model)
                yield {"type": "awaiting_approval", "task_id": task_id}
                yield {"type": "done"}
                return
            history.append(tool_msg)
            summary = ""
            try:
                summary = json.loads(tool_msg["content"]).get("summary", "")
            except Exception:
                pass
            yield {"type": "trace", "event": {"type": "tool_result", "tool": call["name"], "summary": summary}}

        await _checkpoint(task_id, history, iteration, model=effective_model, current_step=iteration)

    # Max iterations inline — promote guidance.
    msg = "This is taking many steps. Try narrowing the request, or ask me to run it as a background task."
    await persist_assistant_message(conversation_id, msg, requester_context)
    yield {"type": "token", "content": msg}
    yield {"type": "done"}
```

> Note: `_save_assistant_message` guards on the conversation existing and links artifacts; passing `id: None` is fine (artifact linking filters on `task_id`). Keep `extract_and_save` imported at module top so the test can monkeypatch `agent_loop.extract_and_save`.

- [ ] **Step 4: Run it (passes)**

Run: `pytest tests/test_chat_turn.py -v`
Expected: PASS (all three)

- [ ] **Step 5: Commit**

```bash
git add runtime/agent_loop.py tests/test_chat_turn.py
git commit -m "feat(runtime): inline streaming chat turn with lazy persistence and start_task promotion"
```

---

## Phase 3 — Rewire the chat router (then HARD CHECKPOINT)

### Task 5: Replace the routing gate with the inline streaming turn

**Files:**
- Modify: `apps/api/routers/chat.py`

- [ ] **Step 1: Edit `send_message`**

In `send_message`, after the `explicit_memory` block and attachment parsing,
**replace** everything from `intent = await classify_intent(...)` through the end
of the function with:

```python
    context = await assemble_context(conversation_id, req.message, requester_context)
    if attachments_context:
        context.append({"role": "user", "content": _format_attachments_for_chat(attachments_context)})

    async def stream():
        async for ev in stream_chat_turn(
            conversation_id=conversation_id,
            message=req.message,
            context_messages=context[:-1],   # assemble_context appends the user msg last; turn re-adds it
            requester_context=requester_context,
            model=req.model,
        ):
            yield f"data: {json.dumps(ev)}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)
```

Add a small helper near `_parse_attachments`:

```python
def _format_attachments_for_chat(attachments: list[dict]) -> str:
    lines = ["# Attached files", "The user attached these files. Their parsed text follows."]
    for a in attachments:
        lines.append(f"\n## {a.get('filename') or 'file'}\n{a.get('preview') or ''}")
    return "\n".join(lines)
```

- [ ] **Step 2: Remove the dead routing code from `chat.py`**

Delete: `_TRIVIAL_CHAT_PHRASES`, `_TOOL_HINT_WORDS`, `_is_trivial_chat`,
`_agent_loop_stream` (the whole fake-streaming function). Remove now-unused imports:
`from core.intent import classify_intent`, `from runtime.agent_loop import format_task_answer`,
`from runtime import task_runner` (if unused), `from runtime.executor import activity_channel`
(if unused). Add `from runtime.agent_loop import stream_chat_turn`. Keep `assemble_context`,
`stream_completion` import only if still referenced (it is not — remove it).

- [ ] **Step 3: Update `test_chat_routing.py`**

The `_is_trivial_chat` tests are obsolete. Replace the whole file with the one
surviving assertion:

```python
# apps/api/tests/test_chat_routing.py
def test_format_task_answer_renders_plain_chat_reply():
    from runtime.agent_loop import format_task_answer
    assert format_task_answer({"answer": "The capital of France is Paris."}) == "The capital of France is Paris."
```

- [ ] **Step 4: Run the chat + turn tests**

Run: `pytest tests/test_chat_routing.py tests/test_chat_turn.py tests/test_streaming_step.py -v`
Expected: PASS. (Intent tests in `test_llm_and_memory.py` will fail at import once `intent.py` is deleted in Phase 4 — not yet.)

- [ ] **Step 5: Commit**

```bash
git add routers/chat.py tests/test_chat_routing.py
git commit -m "feat(chat): stream the agent turn inline; remove the routing gate"
```

### Task 6: 🚦 HARD CHECKPOINT — prove the win before any deletion

- [ ] **Step 1: Start the stack and the API**

```bash
docker-compose up -d
cd apps/api && alembic upgrade head && python seed.py
uvicorn main:app --reload --port 8000
```

- [ ] **Step 2: Manually verify the three scenarios** (curl the SSE endpoint or use the web UI)

1. `"explain how OAuth works"` → tokens stream within ~1–3s; **no `tasks` row** created (verify: `SELECT count(*) FROM tasks WHERE created_at > now() - interval '1 minute';` returns 0).
2. `"what's the latest news on diddy"` → one `tool_call` trace (search), then streamed answer; first token in low single-digit seconds; honest if the search fails.
3. `"research 50 AI law firms and draft outreach"` → model calls `start_task`; a background task is created and queued; activity streams; drafts land in the approval inbox.

- [ ] **Step 3: Record results in the plan; do not proceed to Phase 4 until all three pass.**

If a scenario fails, this is a bug in the new core path — fix it here, while the
diff is small and nothing has been deleted yet.

---

## Phase 4 — Delete the dead machinery (self-verifying)

> **Procedure for every deletion task:** delete the symbol/file → run `pytest -q` →
> every failing test that references the deleted symbol encodes removed behavior →
> delete that test function (or, for mixed files, just the offending function) →
> re-run until green. The pytest failure list is the authority. Do **not** rewrite
> deleted-behavior tests.

### Task 7: Remove `route_tool` and `reasoning_summary` from the loop

**Files:**
- Modify: `apps/api/runtime/agent_loop.py`
- Delete: `apps/api/core/tool_router.py`

- [ ] **Step 1: In `agent_loop.py`** remove: the `route_tool` / `ToolRoutingDecision` import; the iteration-0 `routing_decision` block in `run_loop` (the `if iteration == 0:` route block, lines ~991–1000); the `routing_decision` param on `_llm_step` and its `tool_choice` override (always use `"auto"`); `_append_routing_instruction`; the function `publish_reasoning_summary` and `_summarizer_history_excerpt`; and every `await publish_reasoning_summary(...)` call site in `run_loop`.
- [ ] **Step 2:** `git rm core/tool_router.py`
- [ ] **Step 3:** Run `pytest -q`. Expected failures reference `publish_reasoning_summary`, `route_tool`, or `tool_router`:
  - `tests/test_runtime_reliability_phase1.py::test_reasoning_summary_activity_is_live_only` → delete the function; also remove the two `monkeypatch.setattr(agent_loop, "publish_reasoning_summary", ...)` / `route_tool` lines inside `test_run_loop_gates_write_after_untrusted_prompt_injection` (keep that test — it still asserts governance; it no longer needs those patches).
  - `tests/test_orchestration_category1.py::test_native_loop_confidence_routes_first_tool_call` → delete. In `test_native_loop_adds_controller_replan_instruction_after_tool_error`, remove the `fake_reasoning_summary` + its `monkeypatch.setattr` line (keep the test).
- [ ] **Step 4:** Re-run `pytest -q` until green.
- [ ] **Step 5:** Commit `git add -A && git commit -m "refactor(runtime): drop route_tool pre-call and per-step reasoning summary"`

### Task 8: Delete the DAG executor path

**Files:**
- Modify: `apps/api/runtime/executor.py`

- [ ] **Step 1:** Reduce `TaskExecutor` to the native-loop shim. Replace the class body so:

```python
class TaskExecutor:
    async def run(self, task_id: str) -> None:
        task = await get_task(task_id)
        if not task:
            raise RuntimeError(f"Task not found: {task_id}")
        if task["status"] in {"complete", "failed", "cancelled"}:
            return
        try:
            await save_task(task_id, status="running", started_at=now_utc())
            await run_loop(task)
        except asyncio.CancelledError:
            await save_task(task_id, status="cancelled", error="Task execution was cancelled.", completed_at=now_utc())
            raise
        except Exception as exc:
            error = f"executor_error: {type(exc).__name__}: {exc}"
            log.exception("Task %s crashed in executor", task_id)
            await save_task(task_id, status="failed", error=error, completed_at=now_utc())
            await emit_activity(task_id, {"type": "task_failed", "error": error})
            raise

    async def resume(self, task_id: str) -> None:
        task = await get_task(task_id)
        if not task:
            return
        if task["status"] in {"complete", "failed", "cancelled"}:
            return
        if task["status"] in {"awaiting_approval", "paused"}:
            await resume_after_approval(task_id)
            return
        state = task.get("agent_state") or {}
        if isinstance(state, dict) and state.get("agent_history"):
            await run_loop(task)
        else:
            await self.run(task_id)
```

Then delete everything DAG-only: the `runtime.planner` import; `_run_dag`,
`_resume_dag`, `_execute_dag_step`, `_run_think_step`, `_deterministic_think`,
`_handle_approval_gate`, `_maybe_replan`, `_merge_replan`, `_checkpoint_dag`,
`_preflight_and_route`, `_PausedForApproval`, `_ready_steps`, `_condition_met`,
`_is_fresh_top_level`, `_has_dag_plan`, `_dag_state`, `_updated_dag_state`,
`_stored_model`, `_public_context`, `_maybe_await`, `_task_cancelled`,
`_safe_condition`, `_eval_condition_node`, `_compare_values`, `_to_namespace`,
`_find_context_list`, `resolve_args`, `_resolve_value`, `_lookup_context_path`,
`_lookup_key`, `_TEMPLATE_RE`, and the now-unused imports (`ast`, `re`,
`SimpleNamespace`, `create_checkpoint`, `complete_json`, `tool_broker`).
Keep the re-exported helpers `update_task`, `insert_task`,
`approvals_ready_for_drafting`, `AGENT_LOOP_APPROVAL_STEP_ID`, `now_utc`, and the
re-exports from `agent_loop` (`activity_channel`, `emit_activity`, `get_task`,
`run_loop`, `resume_after_approval`, `save_task`).

- [ ] **Step 2:** Run `pytest -q`. Delete failing DAG tests the procedure surfaces:
  - `tests/test_workflow_runtime.py` → both functions are DAG; delete the file.
  - `tests/test_orchestration_category1.py` → delete the DAG/planner functions: `test_task_executor_runs_ready_dag_steps_in_parallel_and_honors_dependencies`, `test_task_executor_skips_condition_false_and_runs_else_branch`, `test_task_executor_replans_remaining_dag_steps_after_group_completion`, `test_dag_tool_args_resolve_template_references_for_composition`, `test_task_executor_rejects_invalid_plan_before_tool_execution`, `test_run_routes_complex_goal_through_create_plan_to_dag`, `test_run_uses_native_loop_for_non_complex_goal`, `test_run_falls_back_to_native_loop_when_classifier_names_wrong_tools`, `test_run_skips_preflight_for_existing_dag_plan`, and the planner ones (`test_planner_*`, `test_validate_plan_*`). Keep `test_native_loop_adds_controller_replan_instruction_after_tool_error` (already de-summarized in Task 7). If the file is left empty, delete it.
  - `tests/test_state_management_category5.py` → delete `test_resume_routes_dag_task_to_resume_dag`, `test_normalize_plan_preserves_checkpoint_key`, `test_dag_step_with_checkpoint_creates_named_snapshot`, `test_dag_spawn_injects_live_context_into_subagent`. **Keep** the native-loop resume tests (`test_resume_reenters_native_loop_when_history_present`, `test_resume_delegates_to_run_for_unstarted_native_task`, `test_resume_awaiting_approval_uses_resume_after_approval`, `test_resume_ignores_terminal_task`, `test_load_history_*`, `test_resolve_inherited_context_*`).
- [ ] **Step 3:** Re-run `pytest -q` until green.
- [ ] **Step 4:** Commit `git add -A && git commit -m "refactor(runtime): delete DAG executor path; TaskExecutor runs the native loop"`

### Task 9: Delete `runtime/planner.py` and `core/intent.py`

**Files:**
- Delete: `apps/api/runtime/planner.py`, `apps/api/core/intent.py`

- [ ] **Step 1:** `git rm runtime/planner.py core/intent.py`
- [ ] **Step 2:** Run `pytest -q`. Delete failing references the procedure surfaces:
  - `tests/test_llm_and_memory.py` → delete `test_intent_classification_uses_fast_model`, `test_current_web_questions_route_to_task_before_model`, `test_timeless_explanation_questions_stay_chat`, and remove the `from core import intent` / `import intent` usage. **Keep** all `stream_chat_completion`, `embed`, `extract_and_save`, memory, and tool-broker tests.
  - `tests/test_runtime_sprint4.py` and `tests/test_connector_operations.py` → read each failure; delete only functions that import `runtime.planner`/`core.intent`/`tool_router`. **Do not** touch `connectors/framework/planner.py` references.
  - `tests/test_state_management_category5.py` → if any residual `planner.*` reference remains after Task 8, remove it.
- [ ] **Step 3:** Re-run `pytest -q` until green.
- [ ] **Step 4:** Grep to confirm no live imports remain: `grep -rn --include="*.py" -e "core.intent" -e "runtime.planner" -e "tool_router" -e "classify_intent" apps/api` → only `connectors/framework/planner.py` (unrelated) may match `planner`. Expected: no `core.intent` / `runtime.planner` / `tool_router` hits.
- [ ] **Step 5:** Commit `git add -A && git commit -m "refactor: delete intent classifier and DAG planner modules"`

---

## Phase 5 — Polish

### Task 10: Revise the agent system prompt for chat + tasks

**Files:**
- Modify: `apps/api/runtime/agent_loop.py` (`_agent_system_message`)

- [ ] **Step 1: Replace the prompt body** in `_agent_system_message` so the frame fits both quick answers and durable work (keep the search-honesty rules and `{manifest}`):

```python
            "You are Chronos, an enterprise AI assistant. Answer quick questions "
            "directly and conversationally. Use tools when they genuinely help: "
            f"search the web for current/recent/time-sensitive facts (current date: {current_date}), "
            "read or write files, draft emails. Do not narrate tool use you are not doing. "
            "If a request is a large, multi-step, long-running job (deep research, batch "
            "outreach, anything spanning many steps), call start_task to run it as a durable "
            "background task instead of doing it all inline. "
            "All external actions are governed by the broker; some require human approval.\n\n"
            "CRITICAL RULE — Honesty about search results:\n"
            "- If a search returns 0 results, say so. Do not fabricate statistics, sources, or data.\n"
            "- If a tool result contains `is_fallback: true` or a `warning` field, the live search failed. "
            "Report this honestly. Do not present placeholder/fixture data as real.\n"
            "- If you cannot find real data, say \"I could not find that information\" rather than inventing it.\n\n"
            f"{manifest}"
```

> Note: durable `run_loop` uses `ALL_TOOLS` (has `spawn__subagent`, not `start_task`); inline turns use `INLINE_CHAT_TOOLS` (has `start_task`). The manifest is generated from the active tool set, so the prompt's `start_task` guidance only applies where the tool exists — consistent.

- [ ] **Step 2:** Run `pytest -q`. Expected: PASS (no test asserts the exact prompt string; `test_assemble_context_injects_dynamic_tool_manifest` checks the manifest, not this text).
- [ ] **Step 3:** Commit `git add runtime/agent_loop.py && git commit -m "feat(prompt): assistant-first system prompt covering chat and durable tasks"`

### Task 11: Full verification

- [ ] **Step 1:** `pytest -q` → all green.
- [ ] **Step 2:** `ruff check core/ runtime/ routers/ tests/` and `ruff format --check core/ runtime/ routers/` → clean (format touched files if needed).
- [ ] **Step 3:** Re-run the three manual scenarios from Task 6 → still pass.
- [ ] **Step 4:** Confirm durability: start scenario #3, kill `uvicorn` mid-run, restart → the background task resumes (startup `recover_incomplete_tasks` sweep) and completes; drafts still land in approvals.
- [ ] **Step 5:** Final commit if anything changed.

---

## Self-review notes (author)

- **Spec coverage:** deletions (intent/DAG/route/summary) → Tasks 7–9; inline streaming → Tasks 2,4,5; lazy persistence w/ full history → Task 4 (`create_task_from_history`, asserted in `test_first_tool_call_creates_task_with_full_history`); start_task durability boundary → Tasks 3,4; governance unchanged → preserved `_open_approval_gate`/`_needs_approval`, asserted by kept `test_run_loop_gates_write_after_untrusted_prompt_injection`; system prompt → Task 10; bounded memory retrieval → already enforced by `assemble_context`/`memory_retrieve_timeout_seconds` (no code change needed).
- **Type consistency:** `stream_step` terminal events (`token`/`text_done`/`tool_calls`) consumed exactly in `stream_chat_turn`; call dicts use `{id,name,args_str}` matching `_normalise_calls`/`_serialise_assistant`/`_execute_tool`.
- **Open follow-up (out of scope, flagged in spec):** move `fast_model`/`embedding_model` off the free OpenRouter tier; convert the `explicit_memory` shortcut into a model tool.
