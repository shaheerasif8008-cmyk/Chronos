"""Regression test for mixed always-approval batches in the agent loop.

When the model emits a single response mixing an always-approval tool
(gmail__send) with a normal tool (browser__search), the normal sibling
must be executed and its tool result appended to history *before* the
approval gate opens — otherwise its tool_call is orphaned on resume,
because resume_after_approval() only appends results for approval rows.
"""
import json

import pytest

from core.models import ToolResult


def _task() -> dict:
    return {
        "id": "task-mixed-approval",
        "organization_id": "default",
        "region": "us",
        "triggered_by_member_id": "member-1",
        "workspace_id": None,
        "persona_id": None,
        "status": "pending",
        "goal": "Search the web and send an email",
        "depth": 0,
        "iteration_count": 0,
        "agent_state": {},
        "started_at": None,
    }


@pytest.mark.asyncio
async def test_run_loop_executes_normal_sibling_before_gating_always_approval(monkeypatch):
    from runtime import agent_loop

    gmail_call = {
        "id": "call_gmail",
        "name": "gmail__send",
        "args_str": json.dumps({"to": "x@example.com", "subject": "Hi", "body": "yo"}),
    }
    browser_call = {
        "id": "call_browser",
        "name": "browser__search",
        "args_str": json.dumps({"query": "acme"}),
    }

    broker_calls: list[tuple] = []
    gate_capture: dict = {}

    async def fake_llm_step(history, tools, model):
        # Model emits both tools in one response: one always-approval, one normal.
        return None, [gmail_call, browser_call]

    async def fake_execute(agent, tool, args):
        broker_calls.append((tool, args))
        return ToolResult(summary="searched", data={"results": [{"title": "Acme"}]})

    async def fake_open_gate(task, pending_calls, history, iteration, model=None):
        # Capture by copy: history may be mutated by later code paths.
        gate_capture["pending_calls"] = list(pending_calls)
        gate_capture["history"] = list(history)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_loop, "_llm_step", fake_llm_step)
    monkeypatch.setattr(agent_loop.tool_broker, "execute", fake_execute)
    monkeypatch.setattr(agent_loop, "_open_approval_gate", fake_open_gate)
    monkeypatch.setattr(agent_loop, "save_task", noop)
    monkeypatch.setattr(agent_loop, "emit_activity", noop)
    monkeypatch.setattr(agent_loop, "publish_activity", noop)

    result = await agent_loop.run_loop(_task(), tools=agent_loop.ALL_TOOLS, model="test-model")

    assert result == {"status": "awaiting_approval"}

    # The normal sibling executed exactly once, through the broker, in dot notation.
    assert broker_calls == [("browser.search", {"query": "acme"})]

    # "Only gmail__send gets an approval row" → the gate receives exactly the
    # always-approval call and nothing else.
    pending = gate_capture["pending_calls"]
    assert [c["name"] for c in pending] == ["gmail__send"]

    # The browser result is in history *before* the gate, paired by call id,
    # sitting immediately after the assistant message that carried both calls.
    history = gate_capture["history"]
    assistant_idx = next(
        i for i, m in enumerate(history)
        if m.get("role") == "assistant" and m.get("tool_calls")
    )
    tool_call_ids = {tc["id"] for tc in history[assistant_idx]["tool_calls"]}
    assert tool_call_ids == {"call_gmail", "call_browser"}

    sibling_msg = history[assistant_idx + 1]
    assert sibling_msg["role"] == "tool"
    assert sibling_msg["tool_call_id"] == "call_browser"
    assert sibling_msg["name"] == "browser__search"

    # gmail__send must NOT have a tool result yet — it is gated, executed on resume.
    assert not any(
        m.get("role") == "tool" and m.get("tool_call_id") == "call_gmail" for m in history
    )
