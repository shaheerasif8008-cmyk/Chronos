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
async def test_planner_normalizes_dag_plan_with_parallel_groups_and_conditions(monkeypatch):
    from runtime import planner

    async def fake_complete_json(prompt, model=None):
        return json.dumps(
            {
                "steps": [
                    {
                        "id": "search_a",
                        "action": "tool_call",
                        "tool": "browser.search",
                        "args": {"query": "a"},
                        "parallel_group": "research",
                        "output_key": "a_results",
                    },
                    {
                        "id": "fallback",
                        "action": "escalate",
                        "message": "No results found.",
                        "condition": {"if": "len(a_results.results) == 0"},
                        "depends_on": ["search_a"],
                    },
                ],
                "context": {"source": "test"},
            }
        )

    monkeypatch.setattr(planner.settings, "demo_mode", False)
    monkeypatch.setattr(planner, "complete_json", fake_complete_json)

    plan = await planner.create_plan("research a", {"triggered_by": "test"}, "default")

    assert isinstance(plan, dict)
    assert plan["context"] == {"source": "test"}
    assert plan["steps"][0]["parallel_group"] == "research"
    assert plan["steps"][0]["output_key"] == "a_results"
    assert plan["steps"][1]["condition"] == {"if": "len(a_results.results) == 0"}


@pytest.mark.asyncio
async def test_task_executor_runs_ready_dag_steps_in_parallel_and_honors_dependencies(monkeypatch):
    from core.models import ToolResult
    from runtime import executor

    plan = {
        "steps": [
            {"id": "a", "action": "tool_call", "tool": "browser.search", "args": {"query": "a"}, "output_key": "a"},
            {"id": "b", "action": "tool_call", "tool": "browser.search", "args": {"query": "b"}, "output_key": "b"},
            {"id": "join", "action": "think", "prompt": "combine", "depends_on": ["a", "b"], "output_key": "joined"},
        ],
        "context": {},
    }
    task = _task(plan)
    calls = []
    updates = []

    async def fake_get_task(task_id):
        return dict(task)

    async def fake_save_task(task_id, **values):
        updates.append(values)
        task.update(values)

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    async def fake_execute(agent, tool, args):
        calls.append((args["query"], time.perf_counter()))
        await asyncio.sleep(0.05)
        return ToolResult(summary=f"{args['query']} done", data={"query": args["query"]})

    async def fake_think(self, task_arg, step, context, model):
        return {"summary": f"{context['a']['query']}+{context['b']['query']}"}

    monkeypatch.setattr(executor, "get_task", fake_get_task)
    monkeypatch.setattr(executor, "save_task", fake_save_task)
    monkeypatch.setattr(executor, "emit_activity", fake_emit)
    monkeypatch.setattr(executor.tool_broker, "execute", fake_execute)
    monkeypatch.setattr(executor.TaskExecutor, "_run_think_step", fake_think)
    monkeypatch.setattr(executor.TaskExecutor, "_maybe_replan", lambda self, task_arg, completed, context, model: None)

    await executor.TaskExecutor().run("task-dag")

    assert {query for query, _ in calls} == {"a", "b"}
    assert abs(calls[0][1] - calls[1][1]) < 0.03
    assert task["status"] == "complete"
    assert task["result"]["a"] == {"query": "a"}
    assert task["result"]["b"] == {"query": "b"}
    assert task["result"]["joined"] == {"summary": "a+b"}
    assert any(update.get("current_step") == 3 for update in updates)


@pytest.mark.asyncio
async def test_task_executor_skips_condition_false_and_runs_else_branch(monkeypatch):
    from core.models import ToolResult
    from runtime import executor

    plan = {
        "steps": [
            {"id": "search", "action": "tool_call", "tool": "browser.search", "args": {"query": "x"}, "output_key": "raw"},
            {
                "id": "qualify",
                "action": "think",
                "depends_on": ["search"],
                "condition": {"if": "len(raw.results) > 0", "else": "fallback"},
                "output_key": "qualified",
            },
            {"id": "fallback", "action": "escalate", "message": "No results", "depends_on": ["search"]},
        ],
        "context": {},
    }
    task = _task(plan)

    async def fake_get_task(task_id):
        return dict(task)

    async def fake_save_task(task_id, **values):
        task.update(values)

    async def fake_execute(agent, tool, args):
        return ToolResult(summary="empty", data={"results": []})

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    async def fake_think(*args, **kwargs):
        raise AssertionError("condition false step should not run")

    monkeypatch.setattr(executor, "get_task", fake_get_task)
    monkeypatch.setattr(executor, "save_task", fake_save_task)
    monkeypatch.setattr(executor, "emit_activity", fake_emit)
    monkeypatch.setattr(executor.tool_broker, "execute", fake_execute)
    monkeypatch.setattr(executor.TaskExecutor, "_run_think_step", fake_think)
    monkeypatch.setattr(executor.TaskExecutor, "_maybe_replan", lambda self, task_arg, completed, context, model: None)

    await executor.TaskExecutor().run("task-dag")

    assert task["status"] == "failed"
    assert task["error"] == "No results"
    assert "qualified" not in task["result"]


@pytest.mark.asyncio
async def test_task_executor_replans_remaining_dag_steps_after_group_completion(monkeypatch):
    from core.models import ToolResult
    from runtime import executor

    plan = {
        "steps": [
            {"id": "search", "action": "tool_call", "tool": "browser.search", "args": {"query": "x"}, "output_key": "raw"},
            {"id": "old_next", "action": "think", "depends_on": ["search"], "output_key": "old"},
        ],
        "context": {},
    }
    task = _task(plan)

    async def fake_get_task(task_id):
        return dict(task)

    async def fake_save_task(task_id, **values):
        task.update(values)

    async def fake_execute(agent, tool, args):
        return ToolResult(summary="one", data={"results": [{"title": "A"}]})

    async def fake_think(self, task_arg, step, context, model):
        return {"step": step["id"]}

    async def fake_replan(self, task_arg, completed, context, model):
        if completed == {"search"}:
            return [
                {"id": "new_next", "action": "think", "depends_on": ["search"], "output_key": "new"},
            ]
        return None

    monkeypatch.setattr(executor, "get_task", fake_get_task)
    monkeypatch.setattr(executor, "save_task", fake_save_task)
    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    monkeypatch.setattr(executor, "emit_activity", fake_emit)
    monkeypatch.setattr(executor.tool_broker, "execute", fake_execute)
    monkeypatch.setattr(executor.TaskExecutor, "_run_think_step", fake_think)
    monkeypatch.setattr(executor.TaskExecutor, "_maybe_replan", fake_replan)

    await executor.TaskExecutor().run("task-dag")

    assert task["status"] == "complete"
    assert "old" not in task["result"]
    assert task["result"]["new"] == {"step": "new_next"}
    assert [step["id"] for step in task["plan"]["steps"]] == ["search", "new_next"]


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

    async def fake_llm_step(history, tools, model, routing_decision=None):
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


@pytest.mark.asyncio
async def test_native_loop_confidence_routes_first_tool_call(monkeypatch):
    from core.tool_router import ToolRoutingDecision
    from runtime import agent_loop

    task = {
        "id": "task-routed",
        "organization_id": "default",
        "region": "us",
        "triggered_by_member_id": "member-1",
        "workspace_id": "workspace-1",
        "persona_id": None,
        "status": "pending",
        "goal": "what is the latest funding news?",
        "plan": {},
        "agent_state": {},
        "iteration_count": 0,
        "started_at": None,
        "depth": 0,
    }
    seen_decisions = []

    async def fake_route(message, available_tools):
        assert "browser__search" in available_tools
        return ToolRoutingDecision(
            tool="browser__search",
            confidence=0.91,
            reasoning="Latest news requires live search.",
        )

    async def fake_llm_step(history, tools, model, routing_decision=None):
        seen_decisions.append(routing_decision)
        assert any("Tool routing observation" in message.get("content", "") for message in history)
        return "Done.", []

    async def fake_save_task(task_id, **values):
        task.update(values)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_loop, "route_tool", fake_route)
    monkeypatch.setattr(agent_loop, "_llm_step", fake_llm_step)
    monkeypatch.setattr(agent_loop, "save_task", fake_save_task)
    monkeypatch.setattr(agent_loop, "publish_activity", noop)
    monkeypatch.setattr(agent_loop, "emit_activity", noop)
    monkeypatch.setattr(agent_loop, "_persist_to_conversation", noop)

    await agent_loop.run_loop(task)

    assert seen_decisions[0].tool == "browser__search"
    assert seen_decisions[0].confidence == 0.91


def test_dag_tool_args_resolve_template_references_for_composition():
    from runtime.executor import resolve_args

    args = {
        "url": "{{search.results[0].url}}",
        "nested": {"title": "{{search.results[0].title}}"},
        "plain": "unchanged",
    }
    context = {"search": {"results": [{"url": "https://example.com", "title": "Example"}]}}

    assert resolve_args(args, context) == {
        "url": "https://example.com",
        "nested": {"title": "Example"},
        "plain": "unchanged",
    }


@pytest.mark.asyncio
async def test_task_planner_classifies_goal_and_rejects_missing_tools(monkeypatch):
    from runtime import planner

    async def fake_complete_json(prompt, model=None):
        if "Classify this task goal" in prompt:
            return json.dumps(
                {
                    "complexity": "complex",
                    "requires_tools": ["salesforce.search"],
                    "requires_sub_agents": False,
                    "requires_approval": False,
                    "estimated_steps": 4,
                    "success_criteria": "Qualified accounts are listed.",
                }
            )
        raise AssertionError("Planning should stop before asking for a plan when tools are missing")

    monkeypatch.setattr(planner, "complete_json", fake_complete_json)

    with pytest.raises(planner.PlanningError, match="Missing tools"):
        await planner.TaskPlanner(available_tools=["browser.search"]).plan("find accounts")


def test_validate_plan_rejects_unknown_tools_and_missing_approval_gate():
    from runtime import planner

    plan = {
        "steps": [
            {
                "id": "send",
                "action": "tool_call",
                "tool": "gmail.send",
                "args": {"to": "a@example.com"},
                "depends_on": [],
            },
            {
                "id": "missing",
                "action": "tool_call",
                "tool": "salesforce.search",
                "args": {},
                "depends_on": [],
            },
        ],
        "context": {},
    }

    result = planner.validate_plan(plan, available_tools=["gmail.send"])

    assert result.valid is False
    assert any("requires an approval_gate" in error for error in result.errors)
    assert any("salesforce.search" in error for error in result.errors)


def test_validate_plan_warns_when_template_reference_has_no_output_key():
    from runtime import planner

    plan = {
        "steps": [
            {"id": "search", "action": "tool_call", "tool": "browser.search", "args": {}, "depends_on": []},
            {
                "id": "fetch",
                "action": "tool_call",
                "tool": "browser.fetch",
                "args": {"url": "{{search_results.results[0].url}}"},
                "depends_on": ["search"],
            },
        ],
        "context": {},
    }

    result = planner.validate_plan(plan, available_tools=["browser.search", "browser.fetch"])

    assert result.valid is True
    assert any("search_results" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_task_executor_rejects_invalid_plan_before_tool_execution(monkeypatch):
    from runtime import executor

    task = _task(
        {
            "steps": [
                {
                    "id": "bad",
                    "action": "tool_call",
                    "tool": "salesforce.search",
                    "args": {},
                    "depends_on": [],
                }
            ],
            "context": {},
        }
    )

    async def fake_get_task(task_id):
        return dict(task)

    async def fake_save_task(task_id, **values):
        task.update(values)

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    async def fail_execute(*args, **kwargs):
        raise AssertionError("invalid plan should not execute a broker tool")

    monkeypatch.setattr(executor, "get_task", fake_get_task)
    monkeypatch.setattr(executor, "save_task", fake_save_task)
    monkeypatch.setattr(executor, "emit_activity", fake_emit)
    monkeypatch.setattr(executor.tool_broker, "execute", fail_execute)

    await executor.TaskExecutor().run("task-dag")

    assert task["status"] == "failed"
    assert "salesforce.search" in task["error"]
