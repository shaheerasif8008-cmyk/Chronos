"""Integration test: the plan-execute-reflect path through run_loop.

Drives the real run_loop with the planner, critic, and llm step mocked so the
cognitive control flow (plan built + persisted, reflection forces another round,
progress checkpoint) is exercised deterministically without a network.
"""
from __future__ import annotations

import json

import pytest


def _task():
    return {
        "id": "task-cog",
        "organization_id": "default",
        "region": "us",
        "triggered_by_member_id": "member-1",
        "workspace_id": "workspace-1",
        "persona_id": None,
        "status": "pending",
        "goal": "Research competitors and write a positioning memo",
        "plan": {},
        "agent_state": {},
        "iteration_count": 0,
        "current_step": 0,
        "started_at": None,
        "depth": 0,
    }


@pytest.mark.asyncio
async def test_run_loop_plans_then_reflects_before_finishing(monkeypatch):
    from runtime import agent_loop

    task = _task()
    saved: list[dict] = []
    emitted: list[dict] = []

    # Model cognition gate: pretend a model key is configured.
    monkeypatch.setattr(agent_loop.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(agent_loop.settings, "agent_cognition_enabled", True)

    async def fake_build_plan(goal):
        return [
            {"id": "step-1", "action": "think", "description": "Research competitors", "tool": "browser"},
            {"id": "step-2", "action": "think", "description": "Write the memo", "tool": None},
        ]

    # Critic rejects the first answer, accepts the second.
    verdicts = [
        agent_loop.cognition.Verdict(complete=False, missing=["sources"], directive="Add cited sources."),
        agent_loop.cognition.Verdict(complete=True, missing=[], directive=""),
    ]

    async def fake_reflect(goal, answer, summaries):
        return verdicts.pop(0)

    # 1) call a tool, 2) shallow answer (rejected), 3) improved answer (accepted)
    steps = [
        (None, [{"id": "c1", "name": "browser__search", "args_str": json.dumps({"q": "rivals"})}]),
        ("Draft memo v1.", []),
        ("Draft memo v2 with cited sources.", []),
    ]

    async def fake_llm_step(history, tools, model, *, reasoning_effort=None):
        return steps.pop(0)

    async def fake_execute_tool(call, task_arg, agent):
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "name": call["name"],
            "content": json.dumps({"summary": "Found 5 competitors", "data": {}}),
        }

    async def fake_save_task(task_id, **values):
        saved.append(values)
        task.update(values)

    async def fake_emit(task_id, event, actor_id="chronos"):
        emitted.append(event)

    async def fake_publish(task_id, event):
        return None

    async def fake_persist(task_arg, content, **kwargs):
        return "msg-1"

    monkeypatch.setattr(agent_loop, "_build_plan", fake_build_plan)
    monkeypatch.setattr(agent_loop, "_reflect", fake_reflect)
    monkeypatch.setattr(agent_loop, "_llm_step", fake_llm_step)
    monkeypatch.setattr(agent_loop, "_execute_tool", fake_execute_tool)
    monkeypatch.setattr(agent_loop, "save_task", fake_save_task)
    monkeypatch.setattr(agent_loop, "emit_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "publish_activity", fake_publish)
    monkeypatch.setattr(agent_loop, "_persist_to_conversation", fake_persist)

    result = await agent_loop.run_loop(task)

    # The improved (second) answer was accepted, not the shallow first one.
    assert result == {"answer": "Draft memo v2 with cited sources."}
    # Both critic verdicts were consumed (reject → continue, accept → finish).
    assert verdicts == []
    # The plan was persisted to the task row in the UI step shape.
    plan_saves = [s for s in saved if "plan" in s]
    assert plan_saves and plan_saves[0]["plan"]["steps"][0]["description"] == "Research competitors"
    # Plan steps and the reflection retry surfaced as activity events.
    types = [e.get("type") for e in emitted]
    assert "step_start" in types
    assert "step_retry" in types
    assert "task_complete" in types


@pytest.mark.asyncio
async def test_run_loop_without_model_key_skips_cognition(monkeypatch):
    """No model key → planner/critic never run; behavior matches the plain loop."""
    from runtime import agent_loop

    task = _task()
    emitted: list[dict] = []
    monkeypatch.setattr(agent_loop.settings, "openrouter_api_key", "")
    monkeypatch.setattr(agent_loop.settings, "backup_api_key", "")

    async def boom_plan(goal):  # must never be called
        raise AssertionError("planner ran without a model key")

    async def boom_reflect(goal, answer, summaries):
        raise AssertionError("critic ran without a model key")

    steps = [("Direct answer.", [])]

    async def fake_llm_step(history, tools, model, *, reasoning_effort=None):
        return steps.pop(0)

    async def fake_save_task(task_id, **values):
        task.update(values)

    async def fake_emit(task_id, event, actor_id="chronos"):
        emitted.append(event)

    async def fake_publish(task_id, event):
        return None

    async def fake_persist(task_arg, content, **kwargs):
        return None

    monkeypatch.setattr(agent_loop, "_build_plan", boom_plan)
    monkeypatch.setattr(agent_loop, "_reflect", boom_reflect)
    monkeypatch.setattr(agent_loop, "_llm_step", fake_llm_step)
    monkeypatch.setattr(agent_loop, "_execute_tool", lambda *a, **k: None)
    monkeypatch.setattr(agent_loop, "save_task", fake_save_task)
    monkeypatch.setattr(agent_loop, "emit_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "publish_activity", fake_publish)
    monkeypatch.setattr(agent_loop, "_persist_to_conversation", fake_persist)

    result = await agent_loop.run_loop(task)
    assert result == {"answer": "Direct answer."}
    assert "step_start" not in [e.get("type") for e in emitted]


@pytest.mark.asyncio
async def test_run_loop_escalates_model_when_errors_recur(monkeypatch):
    """A recurring tool error makes the loop step up to a stronger model."""
    from runtime import agent_loop

    task = _task()
    task["agent_state"] = {"model": "openrouter/deepseek/deepseek-v4-flash"}
    monkeypatch.setattr(agent_loop.settings, "agent_cognition_enabled", True)
    # Light cognition (progress + routing) does not require a key; keep model
    # cognition off so the planner/critic don't run in this focused test.
    monkeypatch.setattr(agent_loop.settings, "openrouter_api_key", "")
    monkeypatch.setattr(agent_loop.settings, "backup_api_key", "")

    models_seen: list[str] = []
    # Three failing search calls (same error) then a final answer.
    steps = [
        (None, [{"id": "c1", "name": "browser__search", "args_str": json.dumps({"q": "a"})}]),
        (None, [{"id": "c2", "name": "browser__search", "args_str": json.dumps({"q": "b"})}]),
        (None, [{"id": "c3", "name": "browser__search", "args_str": json.dumps({"q": "c"})}]),
        ("Best effort answer.", []),
    ]

    async def fake_llm_step(history, tools, model, *, reasoning_effort=None):
        models_seen.append(model)
        return steps.pop(0)

    async def fake_execute_tool(call, task_arg, agent):
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "name": call["name"],
            "content": json.dumps({"error": "rate limited"}),
        }

    async def fake_save_task(task_id, **values):
        task.update(values)

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    async def fake_publish(task_id, event):
        return None

    async def fake_persist(task_arg, content, **kwargs):
        return None

    monkeypatch.setattr(agent_loop, "_llm_step", fake_llm_step)
    monkeypatch.setattr(agent_loop, "_execute_tool", fake_execute_tool)
    monkeypatch.setattr(agent_loop, "save_task", fake_save_task)
    monkeypatch.setattr(agent_loop, "emit_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "publish_activity", fake_publish)
    monkeypatch.setattr(agent_loop, "_persist_to_conversation", fake_persist)

    result = await agent_loop.run_loop(task)
    assert result == {"answer": "Best effort answer."}
    # Started on flash; escalated to a stronger model after the errors recurred.
    assert models_seen[0] == "openrouter/deepseek/deepseek-v4-flash"
    assert models_seen[-1] != "openrouter/deepseek/deepseek-v4-flash"
