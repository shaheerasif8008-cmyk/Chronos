"""Tests for agent-loop context budgeting + compaction (Category 7).

Focus: compaction must never break tool_call/tool_result pairing, must keep the
result under budget, and must be a safe no-op when already small.
"""
import pytest

from core.token_budget import (
    compact_agent_history,
    estimate_messages_tokens,
    split_into_turns,
)


def _system_and_goal() -> list[dict]:
    return [
        {"role": "system", "content": "You are Chronos."},
        {"role": "user", "content": "Research 50 companies and draft outreach."},
    ]


def _turn(call_id: str, names: list[str], result_size: int = 0) -> list[dict]:
    """Build one assistant(tool_calls)+tool-results turn."""
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": f"{call_id}_{i}", "type": "function",
             "function": {"name": n, "arguments": "{}"}}
            for i, n in enumerate(names)
        ],
    }
    tools = [
        {"role": "tool", "tool_call_id": f"{call_id}_{i}", "name": n,
         "content": "X" * result_size}
        for i, n in enumerate(names)
    ]
    return [assistant, *tools]


def _assert_pairing_valid(history: list[dict]) -> None:
    """Every assistant tool_call id has a matching tool message and vice versa."""
    call_ids: list[str] = []
    for msg in history:
        for call in msg.get("tool_calls") or []:
            call_ids.append(call["id"])
    tool_ids = [m["tool_call_id"] for m in history if m.get("role") == "tool"]
    # No orphan tool messages.
    for tid in tool_ids:
        assert tid in call_ids, f"orphan tool message: {tid}"
    # No dangling tool_calls without a result.
    for cid in call_ids:
        assert cid in tool_ids, f"tool_call without result: {cid}"


@pytest.mark.asyncio
async def test_noop_when_history_fits_budget():
    history = _system_and_goal() + _turn("c1", ["browser__search"])
    out = await compact_agent_history(history, budget_tokens=1_000_000)
    assert out is history  # unchanged reference — true no-op


@pytest.mark.asyncio
async def test_compaction_preserves_pairing_with_parallel_turn():
    history = _system_and_goal()
    # A parallel-tool turn, then several single-tool turns with sizable results.
    history += _turn("c0", ["browser__search", "browser__fetch"], result_size=8_000)
    for i in range(1, 6):
        history += _turn(f"c{i}", ["fs__read"], result_size=8_000)
    history.append({"role": "assistant", "content": "Done."})

    budget = 6_000  # less than the full history (~14k tokens) → forces compaction
    out = await compact_agent_history(history, budget_tokens=budget, keep_recent_turns=2)

    _assert_pairing_valid(out)
    assert estimate_messages_tokens(out) <= budget
    # Preamble preserved.
    assert out[0]["role"] == "system"
    assert out[1]["role"] == "user"
    # A summary message was inserted.
    assert any("Earlier task progress summary" in (m.get("content") or "") for m in out)


@pytest.mark.asyncio
async def test_kept_tail_starts_at_assistant():
    history = _system_and_goal()
    for i in range(8):
        history += _turn(f"c{i}", ["fs__read"], result_size=6_000)

    out = await compact_agent_history(history, budget_tokens=4_000, keep_recent_turns=2)

    # The first message after preamble + summary must be an assistant message,
    # so the kept tail never begins with an orphan tool result.
    summary_index = next(i for i, m in enumerate(out) if "Earlier task progress summary" in (m.get("content") or ""))
    assert out[summary_index + 1]["role"] == "assistant"
    _assert_pairing_valid(out)


@pytest.mark.asyncio
async def test_compaction_is_idempotent():
    history = _system_and_goal()
    for i in range(8):
        history += _turn(f"c{i}", ["fs__read"], result_size=6_000)

    once = await compact_agent_history(history, budget_tokens=4_000, keep_recent_turns=2)
    twice = await compact_agent_history(once, budget_tokens=4_000, keep_recent_turns=2)
    # Compacting an already-compact history that now fits is a no-op.
    assert twice is once or twice == once


@pytest.mark.asyncio
async def test_summarizer_is_used_when_provided():
    history = _system_and_goal()
    for i in range(6):
        history += _turn(f"c{i}", ["fs__read"], result_size=10_000)

    async def fake_summarizer(text: str) -> str:
        return "SHORT SUMMARY"

    out = await compact_agent_history(
        history, budget_tokens=3_000, keep_recent_turns=2, summarizer=fake_summarizer
    )
    assert any(m.get("content") == "[Earlier task progress summary]: SHORT SUMMARY" for m in out)


def test_split_into_turns_groups_assistant_with_its_tools():
    body = _turn("c0", ["a", "b"]) + _turn("c1", ["c"]) + [{"role": "assistant", "content": "final"}]
    turns = split_into_turns(body)
    assert len(turns) == 3
    assert turns[0][0]["role"] == "assistant" and len(turns[0]) == 3  # assistant + 2 tools
    assert turns[1][0]["role"] == "assistant" and len(turns[1]) == 2  # assistant + 1 tool
    assert turns[2] == [{"role": "assistant", "content": "final"}]


@pytest.mark.asyncio
async def test_run_loop_persists_compacted_history_on_resume(monkeypatch):
    """End-to-end: as history grows past budget, the loop compacts and the
    persisted agent_state.agent_history is the compacted (trimmed) version —
    so a restart resumes from the trimmed history, not the unbounded original."""
    from runtime import agent_loop
    from core.config import settings

    # Tight window so a few big tool results force compaction.
    monkeypatch.setattr(settings, "max_context_tokens", 20_000)
    monkeypatch.setattr(settings, "response_reserve_tokens", 4_000)

    task = {
        "id": "task-compact-1",
        "organization_id": "default",
        "region": "us",
        "triggered_by_member_id": "member-1",
        "workspace_id": None,
        "persona_id": None,
        "status": "pending",
        "goal": "long running research task",
        "agent_state": {},
        "iteration_count": 0,
        "depth": 0,
    }

    saved: dict = {}

    async def fake_get_task(task_id):
        return task

    async def fake_save_task(task_id, **values):
        saved.update(values)
        task.update(values)

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    async def fake_persist(*args, **kwargs):
        return None

    async def fake_summarize(text):
        return "condensed summary of earlier work"

    # Emit 6 tool-call turns with big results, then a final answer.
    calls_remaining = [6]

    async def fake_llm_step(history, tools, model):
        if calls_remaining[0] > 0:
            calls_remaining[0] -= 1
            return None, [{"id": f"c{calls_remaining[0]}", "name": "fs__read", "args_str": "{}"}]
        return "All done.", []

    async def fake_execute_tool(call, task_, agent):
        # Big results: 6 turns × ~3.5k tokens ≈ 21k tokens > the ~16k budget,
        # so compaction must fire before the loop finishes.
        return {"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                "content": "RESULT " + "X" * 14_000}

    monkeypatch.setattr(agent_loop, "get_task", fake_get_task)
    monkeypatch.setattr(agent_loop, "save_task", fake_save_task)
    monkeypatch.setattr(agent_loop, "emit_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "publish_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "_persist_to_conversation", fake_persist)
    monkeypatch.setattr(agent_loop, "_summarize", fake_summarize)
    monkeypatch.setattr(agent_loop, "_llm_step", fake_llm_step)
    monkeypatch.setattr(agent_loop, "_execute_tool", fake_execute_tool)

    result = await agent_loop.run_loop(task, tools=[{"type": "function", "function": {"name": "fs__read"}}])

    assert result["answer"] == "All done."
    persisted = saved["agent_state"]["agent_history"]
    # Compaction fired: the persisted history carries a summary message...
    assert any("Earlier task progress summary" in (m.get("content") or "") for m in persisted)
    # ...and is bounded well under what 6 raw 6KB results would have been.
    assert estimate_messages_tokens(persisted) <= 20_000
    # ...and pairing is intact.
    _assert_pairing_valid(persisted)
