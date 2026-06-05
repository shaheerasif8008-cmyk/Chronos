import pytest
import os
import json


os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://chronos:chronos@localhost:5432/chronos")


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
            assert values == ["queued", "pending", "planning", "running"]
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

    async def fake_enqueue_task(task_id):
        scheduled.append(f"queue:{task_id}")

    monkeypatch.setattr(main, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(main, "select", fake_select)
    monkeypatch.setattr(main, "engine", FakeEngine())
    monkeypatch.setattr(main.task_runner, "enqueue_task", fake_enqueue_task)

    task_ids = await main.recover_incomplete_tasks()

    assert task_ids == ["pending-task", "running-task"]
    assert scheduled == ["queue:pending-task", "queue:running-task"]


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
    from runtime import agent_loop

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
            "plan": {},
            "agent_state": {},
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

    async def fake_save_task(task_id, **values):
        updates.append(values)
        tasks[task_id].update(values)

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    async def fake_persist(task_arg, content, **kwargs):
        return None

    async def fake_llm_step(messages, tools, model=None, *, reasoning_effort=None):
        assert tools
        decision = decisions.pop(0)
        if decision["type"] == "tool_call":
            return None, [{"id": "call-1", "name": "browser__search", "args_str": json.dumps(decision["args"])}]
        return decision["result"]["answer"], []

    async def fake_execute(agent, tool, args):
        calls.append((tool, args))
        return ToolResult(summary="searched", data={"results": [{"title": "Acme"}]})

    monkeypatch.setattr(agent_loop, "save_task", fake_save_task)
    monkeypatch.setattr(agent_loop, "publish_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "emit_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "_persist_to_conversation", fake_persist)
    monkeypatch.setattr(agent_loop, "_llm_step", fake_llm_step)
    monkeypatch.setattr(agent_loop.tool_broker, "execute", fake_execute)

    await agent_loop.run_loop(tasks["task-agent-loop"])

    assert calls == [("browser.search", {"query": "acme"})]
    assert tasks["task-agent-loop"]["status"] == "complete"
    assert tasks["task-agent-loop"]["result"] == {"answer": "done"}
    checkpoint = tasks["task-agent-loop"]["agent_state"]["agent_history"]
    assert any(message.get("role") == "tool" for message in checkpoint)
    assert tasks["task-agent-loop"]["iteration_count"] == 2


@pytest.mark.asyncio
async def test_agent_loop_pauses_and_checkpoints_on_approval(monkeypatch):
    from runtime import agent_loop

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
            "plan": {},
            "agent_state": {},
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

    async def fake_save_task(task_id, **values):
        tasks[task_id].update(values)

    async def fake_emit(task_id, event, actor_id="chronos"):
        return None

    async def fake_llm_step(messages, tools, model=None, *, reasoning_effort=None):
        return None, [
            {
                "id": "call-1",
                "name": "gmail__send",
                "args_str": json.dumps({"to": ["a@example.com"], "subject": "Hi", "body": "Hello"}),
            }
        ]

    async def fake_open_approval(task, pending_calls, history, iteration, model=None):
        approval_calls.append((task["id"], pending_calls[0]["name"], list(history)))
        await fake_save_task(task["id"], status="awaiting_approval", agent_state={"agent_history": history})

    monkeypatch.setattr(agent_loop, "save_task", fake_save_task)
    monkeypatch.setattr(agent_loop, "publish_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "emit_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "_llm_step", fake_llm_step)
    monkeypatch.setattr(agent_loop, "_open_approval_gate", fake_open_approval)

    await agent_loop.run_loop(tasks["task-approval-loop"])

    assert tasks["task-approval-loop"]["status"] == "awaiting_approval"
    assert approval_calls[0][1] == "gmail__send"
    assert tasks["task-approval-loop"]["agent_state"]["agent_history"]


@pytest.mark.asyncio
async def test_agent_system_prompt_includes_current_date_for_live_search():
    from runtime import agent_loop

    prompt = (await agent_loop._agent_system_message())["content"]

    assert "Current date:" in prompt
    assert "browser__search" in prompt
    assert "latest" in prompt


@pytest.mark.asyncio
async def test_top_level_prompt_prefers_parallel_subagent_spawns():
    from runtime import agent_loop

    prompt = (await agent_loop._agent_system_message())["content"]

    assert "spawn all useful sub-agents in the same assistant step" in prompt
    assert "Do not spawn one sub-agent, wait for it, then spawn the next" in prompt


@pytest.mark.asyncio
async def test_subagent_prompt_bounds_research_iterations():
    from runtime import agent_loop
    from runtime.tool_registry import SUBAGENT_TOOLS

    prompt = (await agent_loop._agent_system_message(SUBAGENT_TOOLS))["content"]

    assert "Use at most 2-4 model iterations" in prompt
    assert "fetch only the 1-3 most valuable pages" in prompt
    assert "do not keep retrying after rate limits" in prompt


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

    assert result.summary == "LIVE SEARCH UNAVAILABLE — returning 2 placeholder results. Do not treat these as real data."
    assert result.data["tier"] == "fixture"
    assert result.data["is_fallback"] is True
    assert "placeholder data" in result.data["warning"]
    assert len(result.data["results"]) == 2
