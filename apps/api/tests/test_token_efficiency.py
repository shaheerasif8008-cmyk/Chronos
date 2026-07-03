import json

import pytest


def test_token_budget_trims_text_to_estimated_token_limit():
    from core.token_budget import estimate_tokens, trim_to_token_budget

    text = "abcd" * 50

    assert estimate_tokens(text) == 50
    trimmed = trim_to_token_budget(text, 10)
    assert estimate_tokens(trimmed) <= 10
    assert trimmed.endswith("...")


def test_agent_history_compaction_preserves_recent_protocol_and_safety_markers():
    from runtime.agent_loop import compact_agent_history_for_model

    history = [{"role": "system", "content": "system"}, {"role": "user", "content": "goal"}]
    for index in range(6):
        history.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call-{index}",
                        "type": "function",
                        "function": {"name": "browser__search", "arguments": json.dumps({"query": f"q{index}"})},
                    }
                ],
            }
        )
        payload = {"summary": f"summary {index}", "data": {"artifact_id": f"artifact-{index}"}}
        if index == 1:
            payload["prompt_injection"] = {"risk": "prompt_injection"}
        if index == 2:
            payload["idempotency_key"] = "idem-2"
        if index == 3:
            payload["error"] = "rate limited"
        history.append(
            {
                "role": "tool",
                "tool_call_id": f"call-{index}",
                "name": "browser__search",
                "content": json.dumps(payload),
            }
        )

    compacted = compact_agent_history_for_model(history, recent_tool_iterations=2)

    assert compacted[0] == history[0]
    assert compacted[1] == history[1]
    prior = next(message for message in compacted if message.get("content", "").startswith("# Prior Work Summary"))
    assert "summary 0" in prior["content"]
    assert "artifact-0" in prior["content"]
    assert "prompt_injection" in prior["content"]
    assert "idem-2" in prior["content"]
    assert "rate limited" in prior["content"]
    assert compacted[-4:] == history[-4:]
    assert len(json.dumps(compacted)) < len(json.dumps(history))


def test_agent_history_compaction_keeps_pending_approval_pair_exact():
    from runtime.agent_loop import compact_agent_history_for_model

    pending = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "approval-call",
                "type": "function",
                "function": {"name": "gmail__send", "arguments": "{}"},
            }
        ],
    }
    history = [{"role": "system", "content": "system"}, {"role": "user", "content": "goal"}]
    for index in range(4):
        history.extend(
            [
                {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{index}", "type": "function", "function": {"name": "browser__search", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": f"c{index}", "name": "browser__search", "content": json.dumps({"summary": f"done {index}"})},
            ]
        )
    history.append(pending)

    compacted = compact_agent_history_for_model(history, pending_approval_calls=[{"id": "approval-call"}])

    assert compacted[-1] is pending
    assert any(message.get("content", "").startswith("# Prior Work Summary") for message in compacted)


@pytest.mark.asyncio
async def test_run_loop_saves_token_usage_when_daily_limit_disabled(monkeypatch):
    from runtime import agent_loop

    task = {
        "id": "task-token-usage",
        "organization_id": "default",
        "region": "us",
        "triggered_by_member_id": "member-1",
        "workspace_id": "default",
        "persona_id": None,
        "status": "pending",
        "goal": "Search and summarize",
        "plan": {},
        "agent_state": {},
        "current_step": 0,
        "result": {},
        "iteration_count": 0,
        "started_at": None,
        "depth": 0,
    }
    saved = []
    seen_history_lengths = []
    steps = [
        (None, [{"id": "call-1", "name": "browser__search", "args_str": json.dumps({"query": "one"})}], 120),
        ("done", [], 80),
    ]

    async def fake_llm_step(history, tools, model, *, reasoning_effort=None):
        seen_history_lengths.append(len(json.dumps(history)))
        return steps.pop(0)

    async def fake_execute_tool(call, task_arg, agent):
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "name": call["name"],
            "content": json.dumps({"summary": "searched", "data": {"results": [{"title": "Acme"}]}}),
        }

    async def fake_save_task(task_id, **values):
        saved.append(values)
        task.update(values)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_loop.settings, "per_org_daily_token_limit", 0)
    monkeypatch.setattr(agent_loop.settings, "agent_cognition_enabled", False)
    monkeypatch.setattr(agent_loop, "_llm_step", fake_llm_step)
    monkeypatch.setattr(agent_loop, "_execute_tool", fake_execute_tool)
    monkeypatch.setattr(agent_loop, "save_task", fake_save_task)
    monkeypatch.setattr(agent_loop, "publish_activity", noop)
    monkeypatch.setattr(agent_loop, "emit_activity", noop)
    monkeypatch.setattr(agent_loop, "_persist_to_conversation", noop)

    result = await agent_loop.run_loop(task)

    assert result == {"answer": "done"}
    token_usage_saves = [
        update["agent_state"]["token_usage"]
        for update in saved
        if "agent_state" in update and update["agent_state"].get("token_usage")
    ]
    assert token_usage_saves
    assert token_usage_saves[-1]["total_tokens"] == 200
    assert token_usage_saves[-1]["steps"][-1]["tokens"] == 80
    assert len(seen_history_lengths) == 2


@pytest.mark.asyncio
async def test_run_loop_finishes_with_last_answer_when_next_step_would_exceed_budget(monkeypatch):
    from runtime import agent_loop

    last_answer = "Best available answer from gathered evidence."
    task = {
        "id": "task-near-budget",
        "organization_id": "org-budget",
        "region": "us",
        "triggered_by_member_id": "member-1",
        "workspace_id": "default",
        "persona_id": None,
        "status": "pending",
        "goal": "Research and summarize",
        "plan": {},
        "agent_state": {
            "agent_history": [
                {"role": "user", "content": "Research and summarize"},
                {"role": "assistant", "content": last_answer},
                {
                    "role": "system",
                    "content": "Controller self-check: answer needs more work.",
                },
            ],
            "token_usage": {
                "total_tokens": 187_000,
                "steps": [
                    {
                        "iteration": 1,
                        "model": "openrouter/openai/gpt-5.4-mini",
                        "tokens": 16_000,
                        "estimated_prompt_tokens": 7_500,
                    },
                    {
                        "iteration": 2,
                        "model": "openrouter/openai/gpt-5.4-mini",
                        "tokens": 15_500,
                        "estimated_prompt_tokens": 7_200,
                    },
                ],
            },
        },
        "current_step": 0,
        "result": {},
        "iteration_count": 12,
        "started_at": None,
        "depth": 0,
    }
    emitted: list[dict] = []
    persisted: list[str] = []

    async def fail_llm_step(*args, **kwargs):
        raise AssertionError("near-budget task must not call the model again")

    async def fake_enforce_model_budget(org_id, *, model, estimated_tokens=0):
        assert estimated_tokens >= 15_500
        raise agent_loop.GovernanceLimitExceeded("daily token budget exhausted for org org-budget: 187000/200000")

    async def fake_save_task(task_id, **values):
        task.update(values)

    async def fake_emit(task_id, event, actor_id="chronos"):
        emitted.append(event)

    async def fake_publish(task_id, event):
        emitted.append(event)

    async def fake_persist(task_arg, content, **kwargs):
        persisted.append(content)
        return "msg-budget"

    monkeypatch.setattr(agent_loop.settings, "agent_cognition_enabled", False)
    monkeypatch.setattr(agent_loop, "_llm_step", fail_llm_step)
    monkeypatch.setattr(agent_loop, "enforce_model_budget", fake_enforce_model_budget)
    monkeypatch.setattr(agent_loop, "save_task", fake_save_task)
    monkeypatch.setattr(agent_loop, "emit_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "publish_activity", fake_publish)
    monkeypatch.setattr(agent_loop, "_persist_to_conversation", fake_persist)

    result = await agent_loop.run_loop(task)

    assert result == {"answer": last_answer}
    assert task["status"] == "complete"
    assert persisted == [last_answer]
    assert any(event.get("type") == "task_complete" for event in emitted)
