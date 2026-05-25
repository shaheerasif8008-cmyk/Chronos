import pytest


def _native_task(status="running", history=None, plan=None):
    state = {}
    if history is not None:
        state["agent_history"] = history
    return {
        "id": "task-5",
        "organization_id": "default",
        "region": "us",
        "triggered_by_member_id": "member-1",
        "workspace_id": "workspace-1",
        "persona_id": None,
        "status": status,
        "goal": "do the thing",
        "plan": plan if plan is not None else {},
        "agent_state": state,
        "current_step": 0,
        "result": {},
        "started_at": None,
        "depth": 0,
    }


def _wire_resume(monkeypatch, executor, task):
    """Record which resume branch fires. Returns dict of call recorders."""
    calls = {"run_loop": [], "resume_after_approval": [], "resume_dag": [], "run": []}

    async def fake_get_task(task_id):
        return dict(task)

    async def fake_run_loop(t, **kwargs):
        calls["run_loop"].append(t)
        return {}

    async def fake_resume_after_approval(task_id):
        calls["resume_after_approval"].append(task_id)

    async def fake_resume_dag(self, t):
        calls["resume_dag"].append(t)

    async def fake_run(self, task_id):
        calls["run"].append(task_id)

    monkeypatch.setattr(executor, "get_task", fake_get_task)
    monkeypatch.setattr(executor, "run_loop", fake_run_loop)
    monkeypatch.setattr(executor, "resume_after_approval", fake_resume_after_approval)
    monkeypatch.setattr(executor.TaskExecutor, "_resume_dag", fake_resume_dag)
    monkeypatch.setattr(executor.TaskExecutor, "run", fake_run)
    return calls


# ── Step 2: crash resume routing ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_reenters_native_loop_when_history_present(monkeypatch):
    """The bug fix: a crashed native task left `running` resumes from its checkpoint."""
    from runtime import executor

    task = _native_task(status="running", history=[{"role": "user", "content": "hi"}])
    calls = _wire_resume(monkeypatch, executor, task)

    await executor.TaskExecutor().resume("task-5")

    assert calls["run_loop"] and not calls["resume_after_approval"]
    assert not calls["resume_dag"] and not calls["run"]


@pytest.mark.asyncio
async def test_resume_delegates_to_run_for_unstarted_native_task(monkeypatch):
    from runtime import executor

    task = _native_task(status="pending", history=None)  # never accrued history
    calls = _wire_resume(monkeypatch, executor, task)

    await executor.TaskExecutor().resume("task-5")

    assert calls["run"] == ["task-5"]            # fresh run → pre-flight + routing
    assert not calls["run_loop"]


@pytest.mark.asyncio
async def test_resume_routes_dag_task_to_resume_dag(monkeypatch):
    from runtime import executor

    plan = {"steps": [{"id": "s1", "action": "think", "description": "x", "tool": None, "args": {}, "approval_required": False, "depends_on": []}], "context": {}}
    task = _native_task(status="running", history=[{"role": "user", "content": "hi"}], plan=plan)
    calls = _wire_resume(monkeypatch, executor, task)

    await executor.TaskExecutor().resume("task-5")

    assert calls["resume_dag"] and not calls["run_loop"] and not calls["resume_after_approval"]


@pytest.mark.asyncio
async def test_resume_awaiting_approval_uses_resume_after_approval(monkeypatch):
    from runtime import executor

    task = _native_task(status="awaiting_approval", history=[{"role": "user", "content": "hi"}])
    calls = _wire_resume(monkeypatch, executor, task)

    await executor.TaskExecutor().resume("task-5")

    assert calls["resume_after_approval"] == ["task-5"]
    assert not calls["run_loop"] and not calls["run"]


@pytest.mark.asyncio
async def test_resume_ignores_terminal_task(monkeypatch):
    from runtime import executor

    task = _native_task(status="complete", history=[{"role": "user", "content": "hi"}])
    calls = _wire_resume(monkeypatch, executor, task)

    await executor.TaskExecutor().resume("task-5")

    assert not any(calls.values())


# ── Step 3: named checkpoints ────────────────────────────────────────────────


def test_normalize_plan_preserves_checkpoint_key():
    from runtime import planner

    plan = planner.normalize_plan(
        {
            "steps": [
                {"id": "s1", "action": "think", "checkpoint": "phase1", "depends_on": []}
            ],
            "context": {},
        }
    )
    assert plan["steps"][0]["checkpoint"] == "phase1"


@pytest.mark.asyncio
async def test_dag_step_with_checkpoint_creates_named_snapshot(monkeypatch):
    from core.models import ToolResult
    from runtime import executor

    plan = {
        "steps": [
            {
                "id": "s1",
                "action": "tool_call",
                "tool": "browser.search",
                "args": {"query": "x"},
                "output_key": "res",
                "checkpoint": "phase1",
                "depends_on": [],
            }
        ],
        "context": {},
    }
    task = _native_task(status="pending", plan=plan)
    checkpoints = []

    async def fake_get_task(task_id):
        return dict(task)

    async def fake_save_task(task_id, **values):
        task.update(values)

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    async def fake_execute(agent, tool, args):
        return ToolResult(summary="ok", data={"hits": 3})

    async def fake_create_checkpoint(t, name, snapshot, step_index):
        checkpoints.append((name, snapshot, step_index))
        return "cp-1"

    monkeypatch.setattr(executor, "get_task", fake_get_task)
    monkeypatch.setattr(executor, "save_task", fake_save_task)
    monkeypatch.setattr(executor, "emit_activity", fake_emit)
    monkeypatch.setattr(executor.tool_broker, "execute", fake_execute)
    monkeypatch.setattr(executor, "create_checkpoint", fake_create_checkpoint)
    monkeypatch.setattr(executor.TaskExecutor, "_maybe_replan", lambda self, t, c, ctx, m: None)

    await executor.TaskExecutor().run("task-5")

    assert len(checkpoints) == 1
    name, snapshot, step_index = checkpoints[0]
    assert name == "phase1"
    assert step_index == 1
    assert snapshot["res"] == {"hits": 3}      # public context frozen at checkpoint


# ── Step 4: sub-agent state inheritance ──────────────────────────────────────


def test_resolve_inherited_context_passthrough_and_from_result():
    from runtime import agent_loop

    # DAG path: executor already resolved live context.
    direct = agent_loop._resolve_inherited_context(
        {"_inherited_context": {"parent_goal": "g", "parent_context": {"leads": [1]}}},
        {"goal": "g", "result": {}},
    )
    assert direct["parent_context"] == {"leads": [1]}

    # Native path: resolve inherit_keys against the parent's persisted result.
    from_result = agent_loop._resolve_inherited_context(
        {"inherit_keys": ["leads"]},
        {"goal": "g", "result": {"leads": [1], "secret": 2}},
    )
    assert from_result["parent_context"] == {"leads": [1]}
    assert "secret" not in from_result["parent_context"]


def test_resolve_inherited_context_is_none_without_opt_in():
    from runtime import agent_loop

    assert agent_loop._resolve_inherited_context({}, {"goal": "g", "result": {"leads": [1]}}) is None


@pytest.mark.asyncio
async def test_load_history_injects_inherited_block_then_goal(monkeypatch):
    from runtime import agent_loop

    async def fake_system(tools=None):
        return {"role": "system", "content": "sys"}

    monkeypatch.setattr(agent_loop, "_agent_system_message", fake_system)

    task = {
        "goal": "qualify the leads",
        "agent_state": {
            "inherited_context": {"parent_goal": "build a pipeline", "parent_context": {"leads": [{"a": 1}]}}
        },
    }
    history = await agent_loop._load_history(task)

    assert len(history) == 3
    assert history[1]["content"].startswith("# Inherited context from parent task")
    assert "build a pipeline" in history[1]["content"]
    assert "leads" in history[1]["content"]
    assert history[2]["content"] == "qualify the leads"


@pytest.mark.asyncio
async def test_load_history_no_block_without_inheritance(monkeypatch):
    from runtime import agent_loop

    async def fake_system(tools=None):
        return {"role": "system", "content": "sys"}

    monkeypatch.setattr(agent_loop, "_agent_system_message", fake_system)

    history = await agent_loop._load_history({"goal": "just do it", "agent_state": {}})

    assert len(history) == 2
    assert history[1]["content"] == "just do it"


@pytest.mark.asyncio
async def test_dag_spawn_injects_live_context_into_subagent(monkeypatch):
    from runtime import agent_loop, executor

    captured = {}

    async def fake_run_subagent(task, args, depth):
        captured["args"] = args
        return {}

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    monkeypatch.setattr(agent_loop, "_run_subagent", fake_run_subagent)
    monkeypatch.setattr(executor, "emit_activity", fake_emit)

    task = _native_task(status="running")
    step = {"id": "sp", "action": "spawn_sub_agent", "args": {"goal": "sub", "inherit_keys": ["leads"]}}
    context = {"leads": [{"x": 1}], "noise": "ignored"}

    await executor.TaskExecutor()._execute_dag_step(task, step, context, None, None, set(), set())

    inherited = captured["args"]["_inherited_context"]
    assert inherited["parent_context"] == {"leads": [{"x": 1}]}
    assert inherited["parent_goal"] == "do the thing"
