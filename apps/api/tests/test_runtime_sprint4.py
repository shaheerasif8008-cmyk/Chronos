import pytest
import os
import json


os.environ["DATABASE_URL"] = "postgresql+asyncpg://chronos:chronos@localhost:5432/chronos"


@pytest.mark.asyncio
async def test_executor_resumes_from_persisted_current_step_after_restart(monkeypatch):
    from runtime import executor

    task = {
        "id": "task-1",
        "organization_id": "default",
        "region": "us",
        "triggered_by_member_id": "member-1",
        "workspace_id": None,
        "persona_id": None,
        "status": "pending",
        "goal": "two step task",
        "plan": [
            {
                "id": "step-1",
                "action": "think",
                "description": "first step",
                "tool": None,
                "args": {},
                "approval_required": False,
                "depends_on": [],
            },
            {
                "id": "step-2",
                "action": "think",
                "description": "second step",
                "tool": None,
                "args": {},
                "approval_required": False,
                "depends_on": ["step-1"],
            },
        ],
        "current_step": 0,
        "result": {},
        "started_at": None,
    }
    events = []

    async def fake_get_task(task_id):
        assert task_id == "task-1"
        return dict(task)

    async def fake_update_task(task_id, **values):
        assert task_id == "task-1"
        task.update(values)

    async def fake_emit(task_id, event, **kwargs):
        events.append(event)
        if event["type"] == "step_done" and event["step"]["id"] == "step-1":
            raise RuntimeError("process killed after step 1")

    async def fake_permission(*args, **kwargs):
        return True

    monkeypatch.setattr(executor, "get_task", fake_get_task)
    monkeypatch.setattr(executor, "update_task", fake_update_task)
    monkeypatch.setattr(executor, "emit_activity", fake_emit)
    monkeypatch.setattr(executor.permissions, "check", fake_permission)

    with pytest.raises(RuntimeError, match="process killed"):
        await executor.TaskExecutor().run("task-1")

    assert task["current_step"] == 1
    assert "step-1" in task["result"]

    async def emit_without_kill(task_id, event, **kwargs):
        events.append(event)

    monkeypatch.setattr(executor, "emit_activity", emit_without_kill)
    await executor.TaskExecutor().resume("task-1")

    assert task["current_step"] == 2
    assert task["status"] == "complete"
    assert "step-2" in task["result"]
    assert [event["step"]["id"] for event in events if event["type"] == "step_start"] == ["step-1", "step-2"]


@pytest.mark.asyncio
async def test_sub_agent_depth_limit_raises_before_insert(monkeypatch):
    from runtime import sub_agent

    inserted = False

    async def fake_insert_task(values):
        nonlocal inserted
        inserted = True
        return "sub-task-1"

    async def fake_audit_log(*args, **kwargs):
        return "audit-1"

    monkeypatch.setattr(sub_agent, "insert_task", fake_insert_task)
    monkeypatch.setattr(sub_agent.audit, "log", fake_audit_log)

    parent = {
        "id": "task-parent",
        "organization_id": "default",
        "region": "us",
        "triggered_by_member_id": "member-1",
        "workspace_id": None,
        "persona_id": None,
        "depth": 3,
    }

    with pytest.raises(sub_agent.DepthLimitExceeded):
        await sub_agent.SubAgentManager().spawn_and_wait(parent, "too deep")

    assert inserted is False


@pytest.mark.asyncio
async def test_startup_recovery_schedules_only_resumable_tasks(monkeypatch):
    import main

    scheduled = []

    class FakeColumn:
        def in_(self, values):
            assert values == ["pending", "planning", "running"]
            return "status-filter"

    class FakeTasks:
        class c:
            id = "id-column"
            status = FakeColumn()

    class FakeResult:
        def all(self):
            return [("pending-task",), ("running-task",)]

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, stmt):
            return FakeResult()

    class FakeEngine:
        def begin(self):
            return FakeConn()

    class FakeExecutor:
        def resume(self, task_id):
            return f"resume:{task_id}"

    async def fake_reflect_table(name):
        assert name == "tasks"
        return FakeTasks()

    def fake_select(*columns):
        assert columns == ("id-column",)

        class FakeSelect:
            def where(self, clause):
                assert clause == "status-filter"
                return self

        return FakeSelect()

    def fake_create_task(coro):
        scheduled.append(coro)
        return coro

    monkeypatch.setattr(main, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(main, "select", fake_select)
    monkeypatch.setattr(main, "engine", FakeEngine())
    monkeypatch.setattr(main, "TaskExecutor", FakeExecutor)
    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)

    task_ids = await main.recover_incomplete_tasks()

    assert task_ids == ["pending-task", "running-task"]
    assert scheduled == ["resume:pending-task", "resume:running-task"]


@pytest.mark.asyncio
async def test_demo_gmail_drafts_are_written_to_tmp_jsonl(monkeypatch, tmp_path):
    from connectors import gmail

    draft_path = tmp_path / "chronos_demo_drafts.jsonl"
    monkeypatch.setattr(gmail, "DEMO_DRAFTS_PATH", draft_path)

    result = await gmail.gmail_connector._create_demo_draft(
        {"to": "lead@example.com", "subject": "Proof", "body": "Hello"}
    )

    assert result.summary == "Demo draft recorded: demo-draft-1"
    assert result.data["path"] == str(draft_path)
    lines = draft_path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["to"] == "lead@example.com"


def test_approved_approval_without_draft_result_is_ready_for_execution():
    from runtime.executor import approvals_ready_for_drafting

    ready = approvals_ready_for_drafting(
        [
            {"status": "pending", "action_payload": {"to": "pending@example.com"}},
            {"status": "approved", "action_payload": {"to": "approved@example.com"}},
            {"status": "approved", "action_payload": {"to": "done@example.com", "draft_result": {"id": "draft-1"}}},
        ]
    )

    assert [row["action_payload"]["to"] for row in ready] == ["approved@example.com"]


@pytest.mark.asyncio
async def test_agent_loop_uses_model_decisions_and_broker_checkpoint(monkeypatch):
    from core.models import ToolResult
    from runtime import executor

    tasks = {
        "task-agent-loop": {
            "id": "task-agent-loop",
            "organization_id": "default",
            "region": "us",
            "triggered_by_member_id": "member-1",
            "workspace_id": "default",
            "persona_id": None,
            "status": "pending",
            "goal": "Search and summarize",
            "plan": {
                "suggested_steps": [
                    {"id": "search", "action": "tool_call", "tool": "browser.search", "args": {"query": "acme"}, "description": "Search"}
                ],
                "agent_history": [],
            },
            "current_step": 0,
            "result": {},
            "iteration_count": 0,
            "started_at": None,
            "depth": 0,
        }
    }
    updates = []
    calls = []
    decisions = [
        {"type": "tool_call", "tool": "browser.search", "args": {"query": "acme"}},
        {"type": "final", "result": {"answer": "done"}},
    ]

    async def fake_get_task(task_id):
        return dict(tasks[task_id])

    async def fake_update_task(task_id, **values):
        updates.append(values)
        tasks[task_id].update(values)

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    async def fake_tool_call(messages, tools, model=None):
        assert tools
        return decisions.pop(0)

    async def fake_execute(agent, tool, args):
        calls.append((tool, args))
        return ToolResult(summary="searched", data={"results": [{"title": "Acme"}]})

    monkeypatch.setattr(executor, "get_task", fake_get_task)
    monkeypatch.setattr(executor, "update_task", fake_update_task)
    monkeypatch.setattr(executor, "emit_activity", fake_emit)
    monkeypatch.setattr(executor.llm, "tool_call", fake_tool_call)
    monkeypatch.setattr(executor.tool_broker, "execute", fake_execute)

    await executor.TaskExecutor()._run_loop("task-agent-loop")

    assert calls == [("browser.search", {"query": "acme"})]
    assert tasks["task-agent-loop"]["status"] == "complete"
    assert tasks["task-agent-loop"]["result"] == {"answer": "done"}
    checkpoint = tasks["task-agent-loop"]["plan"]["agent_history"]
    assert any(message.get("role") == "tool" for message in checkpoint)
    assert tasks["task-agent-loop"]["iteration_count"] == 2


@pytest.mark.asyncio
async def test_agent_loop_pauses_and_checkpoints_on_approval(monkeypatch):
    from core.exceptions import ApprovalRequired
    from runtime import executor

    tasks = {
        "task-approval-loop": {
            "id": "task-approval-loop",
            "organization_id": "default",
            "region": "us",
            "triggered_by_member_id": "member-1",
            "workspace_id": "default",
            "persona_id": None,
            "status": "pending",
            "goal": "Send email",
            "plan": {"suggested_steps": [], "agent_history": []},
            "current_step": 0,
            "result": {},
            "iteration_count": 0,
            "started_at": None,
            "depth": 0,
        }
    }
    approval_calls = []

    async def fake_get_task(task_id):
        return dict(tasks[task_id])

    async def fake_update_task(task_id, **values):
        tasks[task_id].update(values)

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    async def fake_tool_call(messages, tools, model=None):
        return {"type": "tool_call", "tool": "gmail.send", "args": {"to": ["a@example.com"], "subject": "Hi", "body": "Hello"}}

    async def fake_execute(agent, tool, args):
        raise ApprovalRequired(tool, "needs approval")

    async def fake_open_approval(self, task, decision, history):
        approval_calls.append((task["id"], decision["tool"], list(history)))
        await fake_update_task(task["id"], status="awaiting_approval", plan={**task["plan"], "agent_history": history})

    monkeypatch.setattr(executor, "get_task", fake_get_task)
    monkeypatch.setattr(executor, "update_task", fake_update_task)
    monkeypatch.setattr(executor, "emit_activity", fake_emit)
    monkeypatch.setattr(executor.llm, "tool_call", fake_tool_call)
    monkeypatch.setattr(executor.tool_broker, "execute", fake_execute)
    monkeypatch.setattr(executor.TaskExecutor, "_open_approval_checkpoint", fake_open_approval)

    await executor.TaskExecutor()._run_loop("task-approval-loop")

    assert tasks["task-approval-loop"]["status"] == "awaiting_approval"
    assert approval_calls[0][1] == "gmail.send"
    assert tasks["task-approval-loop"]["plan"]["agent_history"]


@pytest.mark.asyncio
async def test_approval_gate_waits_for_all_pending_decisions_before_drafting(monkeypatch):
    from runtime import executor

    task = {
        "id": "task-approval-batch",
        "organization_id": "default",
        "region": "us",
        "triggered_by_member_id": "member-1",
        "workspace_id": None,
        "persona_id": None,
        "status": "awaiting_approval",
        "result": {},
    }
    step = {"id": "approve_drafts", "tool": "gmail.draft"}
    events = []
    updates = []
    executed = False

    class FakeColumn:
        def __eq__(self, other):
            return ("eq", other)

    class FakeApprovals:
        class c:
            task_id = FakeColumn()
            step_id = FakeColumn()

        def where(self, *args):
            return self

    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return [
                {"id": "approval-1", "status": "approved", "action_payload": {"to": "approved@example.com"}},
                {"id": "approval-2", "status": "pending", "action_payload": {"to": "pending@example.com"}},
            ]

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, stmt):
            return FakeResult()

    class FakeEngine:
        def begin(self):
            return FakeConn()

    async def fake_reflect_table(name):
        assert name == "approvals"
        return FakeApprovals()

    async def fake_update_task(task_id, **values):
        updates.append((task_id, values))

    async def fake_emit(task_id, event, **kwargs):
        events.append(event)

    async def fake_execute_approved_drafts(self, task_arg, step_arg, rows):
        nonlocal executed
        executed = True
        return []

    monkeypatch.setattr(executor, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(executor, "engine", FakeEngine())
    monkeypatch.setattr(executor, "update_task", fake_update_task)
    monkeypatch.setattr(executor, "emit_activity", fake_emit)
    monkeypatch.setattr(executor, "select", lambda table: table)
    monkeypatch.setattr(executor.TaskExecutor, "_execute_approved_drafts", fake_execute_approved_drafts)

    with pytest.raises(executor._PausedForApproval):
        await executor.TaskExecutor()._handle_approval_gate(task, step)

    assert executed is False
    assert updates == [("task-approval-batch", {"status": "awaiting_approval"})]
    assert events == []


@pytest.mark.asyncio
async def test_planner_falls_back_to_demo_plan_when_model_fails(monkeypatch):
    from runtime import planner

    async def fake_complete_json(prompt, model=None):
        assert model == planner.settings.agent_model
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(planner, "complete_json", fake_complete_json)
    monkeypatch.setattr(planner.settings, "demo_mode", False)

    plan = await planner.create_plan("research leads and draft outreach", {"triggered_by": "test"}, "default")

    assert [step["action"] for step in plan] == ["spawn_sub_agent", "think", "approval_gate"]
    assert plan[-1]["tool"] == "gmail.draft"


@pytest.mark.asyncio
async def test_planner_falls_back_to_research_plan_for_market_brief(monkeypatch):
    from runtime import planner

    async def fake_complete_json(prompt, model=None):
        assert model == planner.settings.agent_model
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(planner, "complete_json", fake_complete_json)
    monkeypatch.setattr(planner.settings, "demo_mode", False)

    plan = await planner.create_plan(
        "Research the data observability market — top 5 players and how they compare.",
        {"triggered_by": "test"},
        "default",
    )

    assert [step["action"] for step in plan] == ["tool_call", "think"]
    assert plan[0]["tool"] == "browser.search"
    assert plan[1]["id"] == "synthesize"


@pytest.mark.asyncio
async def test_planner_falls_back_to_browser_search_for_current_news(monkeypatch):
    from runtime import planner

    async def fake_complete_json(prompt, model=None):
        assert model == planner.settings.agent_model
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(planner, "complete_json", fake_complete_json)
    monkeypatch.setattr(planner.settings, "demo_mode", False)

    plan = await planner.create_plan(
        "what is the latest news on AI agents?",
        {"triggered_by": "test"},
        "default",
    )

    assert [step["action"] for step in plan] == ["tool_call", "think"]
    assert plan[0]["tool"] == "browser.search"
    assert plan[0]["args"]["query"] == "what is the latest news on AI agents?"


def test_agent_system_prompt_includes_current_date_for_live_search():
    from runtime import agent_loop

    prompt = agent_loop._agent_system_message()["content"]

    assert "Current date:" in prompt
    assert "browser__search" in prompt
    assert "latest" in prompt


def test_observability_skips_langfuse_callback_when_package_missing(monkeypatch):
    import importlib.util

    import litellm
    import main
    from core.config import settings

    litellm.success_callback = []
    litellm.failure_callback = []
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-test")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args, **kwargs):
        if name == "langfuse":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    main._init_observability()

    assert "langfuse" not in litellm.success_callback
    assert "langfuse" not in litellm.failure_callback


@pytest.mark.asyncio
async def test_spawn_sub_agent_step_is_not_retried(monkeypatch):
    from runtime import executor

    attempts = []
    events = []

    async def fake_execute_step(self, task, step):
        attempts.append(step["id"])
        raise RuntimeError("child failed")

    async def fake_emit(task_id, event, **kwargs):
        events.append(event)

    monkeypatch.setattr(executor.TaskExecutor, "_execute_step", fake_execute_step)
    monkeypatch.setattr(executor, "emit_activity", fake_emit)

    with pytest.raises(executor.TaskExecutionError, match="after 1 attempts"):
        await executor.TaskExecutor()._execute_with_retries(
            {"id": "task-1"},
            {"id": "research", "action": "spawn_sub_agent"},
        )

    assert attempts == ["research"]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_browser_search_operator_workflow_fixture_avoids_live_duckduckgo(monkeypatch):
    from connectors import browser

    async def fail_new_page():
        raise AssertionError("operator workflow proof search should not open a browser")

    monkeypatch.setattr(browser.settings, "demo_mode", False)
    monkeypatch.setattr(browser, "_new_page", fail_new_page)

    result = await browser.browser_connector._search(
        {"query": "operator workflow proof: draft approvals", "max_results": 3, "fixture": "operator_workflow_proof"}
    )

    assert result.summary == "Fixture search 'operator workflow proof: draft approvals': 3 leads"
    assert len(result.data["leads"]) == 3
    assert result.data["leads"][0]["domain"] == "demosaas01.example.com"


@pytest.mark.asyncio
async def test_browser_screenshot_creates_missing_minio_bucket(monkeypatch):
    import sys
    import types

    from connectors import browser

    calls = []

    class FakePage:
        async def screenshot(self, full_page=False):
            return b"png"

    class FakeMinio:
        def __init__(self, *args, **kwargs):
            pass

        def bucket_exists(self, bucket):
            calls.append(("bucket_exists", bucket))
            return False

        def make_bucket(self, bucket):
            calls.append(("make_bucket", bucket))

        def put_object(self, bucket, object_name, data, length, content_type):
            calls.append(("put_object", bucket, length, content_type))

    monkeypatch.setitem(sys.modules, "minio", types.SimpleNamespace(Minio=FakeMinio))

    object_name = await browser._save_screenshot(FakePage(), "fetch")

    assert object_name and object_name.startswith("browser-screenshots/")
    assert ("bucket_exists", browser.settings.minio_bucket) in calls
    assert ("make_bucket", browser.settings.minio_bucket) in calls
    assert ("put_object", browser.settings.minio_bucket, 3, "image/png") in calls


@pytest.mark.asyncio
async def test_browser_search_falls_back_to_fixture_results_on_live_timeout(monkeypatch):
    from connectors import browser

    class FakePage:
        async def goto(self, *args, **kwargs):
            raise RuntimeError("duckduckgo timeout")

    class FakeClosable:
        async def close(self):
            return None

    class FakePlaywright:
        async def stop(self):
            return None

    async def fake_new_page():
        return FakePlaywright(), FakeClosable(), FakeClosable(), FakePage()

    monkeypatch.setattr(browser.settings, "demo_mode", False)
    monkeypatch.setattr(browser.settings, "tavily_api_key", "")
    monkeypatch.setattr(browser, "_new_page", fake_new_page)

    result = await browser.browser_connector._search({"query": "data observability market", "max_results": 2})

    assert result.summary == "Browser search fallback 'data observability market': 2 fixture results"
    assert result.data["tier"] == "fixture"
    assert len(result.data["results"]) == 2


@pytest.mark.asyncio
async def test_operator_workflow_proof_task_api_reaches_pending_approval(monkeypatch):
    from core.models import Member, ToolResult
    from routers import tasks
    from runtime import executor

    task_id = "operator-proof-task"
    task_state = {}
    events = []
    approvals = []
    scheduled = []

    async def fake_create_task_record(*, goal, member, triggered_by, persona_id=None, workspace_id=None):
        plan = await tasks.create_plan(goal, {"triggered_by": triggered_by}, member.organization_id)
        task_state.update(
            {
                "id": task_id,
                "organization_id": member.organization_id,
                "region": member.region,
                "triggered_by_member_id": member.id,
                "workspace_id": workspace_id,
                "persona_id": persona_id,
                "status": "pending",
                "goal": goal,
                "plan": plan,
                "current_step": 0,
                "result": {},
                "started_at": None,
            }
        )
        return task_id

    def fake_create_task(coro):
        scheduled.append(coro)
        return coro

    async def fake_get_task(requested_task_id):
        assert requested_task_id == task_id
        return dict(task_state)

    async def fake_update_task(requested_task_id, **values):
        assert requested_task_id == task_id
        task_state.update(values)

    async def fake_emit(requested_task_id, event, **kwargs):
        assert requested_task_id == task_id
        events.append(event)

    async def fake_permission(*args, **kwargs):
        return True

    async def fake_tool_execute(agent, tool, args):
        assert tool == "browser.search"
        assert args["fixture"] == "operator_workflow_proof"
        return ToolResult(
            summary="Fixture search 'operator workflow proof': 2 leads",
            data={
                "leads": [
                    {
                        "company": "DemoSaaS 01",
                        "domain": "demosaas01.example.com",
                        "personalization": "Reference their sales hiring motion.",
                    },
                    {
                        "company": "DemoSaaS 02",
                        "domain": "demosaas02.example.com",
                        "personalization": "Reference their sales hiring motion.",
                    },
                ]
            },
        )

    async def fake_create_approvals(self, task, step):
        approvals.append({"task_id": task["id"], "step_id": step["id"], "draft_count": len(task["result"]["drafts"])})
        return ["approval-1", "approval-2"]

    async def fake_handle_approval_gate(self, task, step):
        approval_ids = await fake_create_approvals(self, task, step)
        await executor.update_task(task["id"], status="awaiting_approval")
        await executor.emit_activity(
            task["id"],
            {"type": "awaiting_approval", "approval_ids": approval_ids, "step_id": step["id"]},
        )
        raise executor._PausedForApproval()

    monkeypatch.setattr(tasks, "create_task_record", fake_create_task_record)
    monkeypatch.setattr(tasks.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(executor, "get_task", fake_get_task)
    monkeypatch.setattr(executor, "update_task", fake_update_task)
    monkeypatch.setattr(executor, "emit_activity", fake_emit)
    monkeypatch.setattr(executor.permissions, "check", fake_permission)
    monkeypatch.setattr(executor.tool_broker, "execute", fake_tool_execute)
    monkeypatch.setattr(executor.TaskExecutor, "_handle_approval_gate", fake_handle_approval_gate)

    response = await tasks.create_task(
        tasks.CreateTaskRequest(
            goal="operator workflow proof: research leads, draft outreach, and request approval",
            conversation_id="conversation-1",
        ),
        Member(id="member-1", organization_id="default", region="us", email="operator@example.com"),
    )

    assert response == {"task_id": task_id}
    assert len(scheduled) == 1

    await scheduled[0]

    assert task_state["status"] == "awaiting_approval", task_state.get("error")
    assert [step["action"] for step in task_state["plan"]] == ["tool_call", "think", "approval_gate"]
    assert task_state["plan"][0]["tool"] == "browser.search"
    assert task_state["plan"][0]["args"]["fixture"] == "operator_workflow_proof"
    assert len(task_state["result"]["leads"]) == 2
    assert len(task_state["result"]["drafts"]) == 2
    assert approvals == [{"task_id": task_id, "step_id": "proof_approval", "draft_count": 2}]
    assert events[-1] == {"type": "awaiting_approval", "approval_ids": ["approval-1", "approval-2"], "step_id": "proof_approval"}


@pytest.mark.asyncio
async def test_chat_task_intent_routes_to_executor_without_chat_completion(monkeypatch):
    from core.models import Member
    from routers import chat

    scheduled = []
    saved = []

    async def fake_save_message(conversation_id, role, content):
        saved.append((conversation_id, role, content))

    async def fake_classify(message):
        return {"mode": "task", "confidence": 0.9, "goal": "research leads and draft outreach"}

    async def fake_create_task_record(*, goal, member, triggered_by, persona_id=None, workspace_id=None):
        assert goal == "research leads and draft outreach"
        assert persona_id == "sdr-outreach"
        assert triggered_by == "conversation-1"
        return "task-1"

    class FakeExecutor:
        def run(self, task_id):
            assert task_id == "task-1"
            return "run-task-1"

    def fake_create_task(coro):
        scheduled.append(coro)
        return coro

    async def fake_stream_completion(*args, **kwargs):
        raise AssertionError("task-mode request should not stream chat completion")
        yield ""

    async def fake_audit_log(*args, **kwargs):
        return "audit-1"

    monkeypatch.setattr(chat, "_save_message", fake_save_message)
    monkeypatch.setattr(chat, "classify_intent", fake_classify)
    monkeypatch.setattr(chat, "TaskExecutor", FakeExecutor)
    monkeypatch.setattr(chat.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(chat, "stream_completion", fake_stream_completion)
    monkeypatch.setattr(chat.audit, "log", fake_audit_log)

    import routers.tasks as tasks_router

    monkeypatch.setattr(tasks_router, "create_task_record", fake_create_task_record)

    response = await chat.send_message(
        chat.ChatRequest(
            message="Can you pull together a lead brief and draft outreach?",
            conversation_id="conversation-1",
            persona_id="sdr-outreach",
        ),
        Member(id="member-1", organization_id="default", region="us", email="operator@example.com"),
    )

    body = ""
    async for chunk in response.body_iterator:
        body += chunk.decode() if isinstance(chunk, bytes) else chunk

    assert "task_created" in body
    assert "task-1" in body
    assert scheduled == ["run-task-1"]
    assert saved[0] == ("conversation-1", "user", "Can you pull together a lead brief and draft outreach?")
    assert saved[-1][1] == "assistant"
