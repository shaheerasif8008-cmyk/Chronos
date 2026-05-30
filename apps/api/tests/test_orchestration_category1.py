import asyncio
import json
import time

import pytest


def _task(plan):
    return {
        "id": "task-dag",
        "organization_id": "default",
        "region": "us",
        "triggered_by_member_id": "member-1",
        "workspace_id": "workspace-1",
        "persona_id": None,
        "status": "pending",
        "goal": "run a graph",
        "plan": plan,
        "agent_state": {},
        "current_step": 0,
        "result": {},
        "started_at": None,
        "depth": 0,
    }


@pytest.mark.asyncio
async def test_native_loop_adds_controller_replan_instruction_after_tool_error(monkeypatch):
    from runtime import agent_loop

    task = {
        "id": "task-native",
        "organization_id": "default",
        "region": "us",
        "triggered_by_member_id": "member-1",
        "workspace_id": "workspace-1",
        "persona_id": None,
        "status": "pending",
        "goal": "search and recover",
        "plan": {},
        "agent_state": {},
        "iteration_count": 0,
        "started_at": None,
        "depth": 0,
    }
    updates = []
    step_calls = [
        (
            None,
            [{"id": "call-1", "name": "browser__search", "args_str": json.dumps({"query": "bad"})}],
        ),
        ("Recovered after changing strategy.", []),
    ]

    async def fake_save_task(task_id, **values):
        updates.append(values)
        task.update(values)

    async def fake_publish(task_id, event):
        return None

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    async def fake_persist(task_arg, content):
        return None

    async def fake_llm_step(history, tools, model):
        if len(step_calls) == 1:
            assert any(
                message.get("role") == "system" and "revise the next action" in message.get("content", "")
                for message in history
            )
        return step_calls.pop(0)

    async def fake_execute_tool(call, task_arg, agent):
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "name": call["name"],
            "content": json.dumps({"error": "temporary search failure"}),
        }

    monkeypatch.setattr(agent_loop, "save_task", fake_save_task)
    monkeypatch.setattr(agent_loop, "publish_activity", fake_publish)
    monkeypatch.setattr(agent_loop, "emit_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "_persist_to_conversation", fake_persist)
    monkeypatch.setattr(agent_loop, "_llm_step", fake_llm_step)
    monkeypatch.setattr(agent_loop, "_execute_tool", fake_execute_tool)

    result = await agent_loop.run_loop(task)

    assert result == {"answer": "Recovered after changing strategy."}
    checkpoint = next(update for update in updates if update.get("agent_state", {}).get("orchestration_state"))
    state = checkpoint["agent_state"]["orchestration_state"]
    assert state["mode"] == "model_native"
    assert state["needs_replan"] is True
    assert state["last_tool_errors"] == [{"tool": "browser__search", "error": "temporary search failure"}]
