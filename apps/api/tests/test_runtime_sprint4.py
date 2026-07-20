import pytest
import os
import json
import uuid


os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://chronos:chronos@localhost:5432/chronos")


async def _async_none():
    return None


def test_agent_context_normalizes_asyncpg_uuid_identifiers():
    from core.models import AgentContext

    identifiers = {
        name: uuid.uuid4()
        for name in (
            "id",
            "organization_id",
            "triggered_by_member_id",
            "workspace_id",
            "project_id",
            "persona_id",
        )
    }

    context = AgentContext.from_task(identifiers)

    assert context.id == f"task:{identifiers['id']}"
    assert context.task_id == str(identifiers["id"])
    assert context.org_id == str(identifiers["organization_id"])
    assert context.member_id == str(identifiers["triggered_by_member_id"])
    assert context.workspace_id == str(identifiers["workspace_id"])
    assert context.project_id == str(identifiers["project_id"])
    assert context.persona_id == str(identifiers["persona_id"])


def test_agent_context_preserves_optional_identifier_defaults():
    from core.models import AgentContext

    context = AgentContext.from_task({"id": uuid.uuid4()})

    assert context.org_id == "default"
    assert context.member_id == "chronos"
    assert context.workspace_id is None
    assert context.project_id is None
    assert context.persona_id is None


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
        {"to": "lead@example.com", "subject": "Proof", "body": "Hello"}, "org-1"
    )

    # Demo drafts are namespaced per tenant so one org's drafts never land in
    # another org's file.
    expected_path = tmp_path / "chronos_demo_drafts.org-1.jsonl"
    assert result.summary == "Demo draft recorded: demo-draft-1"
    assert result.data["path"] == str(expected_path)
    lines = expected_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["to"] == "lead@example.com"
    assert record["organization_id"] == "org-1"

    # A second tenant writing a draft gets its own file and its own counter.
    other = await gmail.gmail_connector._create_demo_draft(
        {"to": "other@example.com", "subject": "Other", "body": "Hi"}, "org-2"
    )
    assert other.data["id"] == "demo-draft-1"
    assert other.data["path"] == str(tmp_path / "chronos_demo_drafts.org-2.jsonl")


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
            return None, [{"id": "call-1", "name": "browser__search", "args_str": json.dumps(decision["args"])}], 0
        return decision["result"]["answer"], [], 0

    async def fake_execute(agent, tool, args):
        calls.append((tool, args))
        return ToolResult(summary="searched", data={"results": [{"title": "Acme"}]})

    monkeypatch.setattr(agent_loop, "save_task", fake_save_task)
    monkeypatch.setattr(agent_loop, "publish_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "emit_activity", fake_emit)
    monkeypatch.setattr(agent_loop, "_persist_to_conversation", fake_persist)
    monkeypatch.setattr(agent_loop, "_llm_step", fake_llm_step)
    monkeypatch.setattr(agent_loop.tool_broker, "execute", fake_execute)
    monkeypatch.setattr(agent_loop, "_verify_answer", lambda *a, **k: _async_none())
    monkeypatch.setattr(agent_loop, "_reflect", lambda *a, **k: _async_none())

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
        ], 0

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


def test_observability_warns_when_langfuse_package_missing_in_development(monkeypatch, caplog):
    import importlib.util

    import litellm
    import main
    from core.config import settings

    monkeypatch.setattr(litellm, "callbacks", [])
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-test")
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args, **kwargs):
        if name == "langfuse":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    main._init_observability()

    assert "langfuse_otel" not in litellm.callbacks
    assert "Failed to initialize configured Langfuse observability" in caplog.text


def test_observability_registers_langfuse_v4_otel_callback(monkeypatch):
    import litellm
    import main
    from core.config import settings

    monkeypatch.setattr(litellm, "callbacks", ["existing"])
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-test")
    monkeypatch.setattr(settings, "langfuse_host", "https://langfuse.example/")
    monkeypatch.setattr(settings, "sentry_dsn", "")

    main._init_observability()
    main._init_observability()

    assert litellm.callbacks == ["existing", "langfuse_otel"]
    assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-test"
    assert os.environ["LANGFUSE_SECRET_KEY"] == "sk-test"
    assert os.environ["LANGFUSE_OTEL_HOST"] == "https://langfuse.example"
    assert os.environ["LANGFUSE_HOST"] == "https://langfuse.example"


def test_observability_fails_closed_when_configured_sdk_missing_in_production(monkeypatch):
    import importlib.util

    import main
    from core.config import settings

    monkeypatch.setattr(settings, "langfuse_public_key", "pk-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-test")
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "sentry_dsn", "")
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args, **kwargs):
        if name == "langfuse":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    with pytest.raises(RuntimeError, match="configured Langfuse"):
        main._init_observability()


def test_observability_initializes_sentry_with_environment_and_no_default_pii(monkeypatch):
    import sentry_sdk

    import main
    from core.config import settings

    captured: dict = {}
    monkeypatch.setattr(settings, "langfuse_public_key", "")
    monkeypatch.setattr(settings, "langfuse_secret_key", "")
    monkeypatch.setattr(settings, "sentry_dsn", "https://public@example.invalid/1")
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: captured.update(kwargs))

    main._init_observability()

    assert captured["dsn"] == "https://public@example.invalid/1"
    assert captured["environment"] == "staging"
    assert captured["send_default_pii"] is False
    assert captured["traces_sample_rate"] == 0.1


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
async def test_browser_screenshot_creates_missing_s3_bucket(monkeypatch):
    import sys
    import types

    from connectors import browser

    calls = []

    class FakePage:
        async def screenshot(self, full_page=False):
            return b"png"

    class FakeS3Client:
        def __init__(self):
            pass

        def head_bucket(self, Bucket):
            calls.append(("head_bucket", Bucket))
            raise Exception("missing")

        def create_bucket(self, **kwargs):
            calls.append(("create_bucket", kwargs))

        def put_object(self, Bucket, Key, Body, ContentType):
            calls.append(("put_object", Bucket, Key, len(Body), ContentType))

    fake_client = FakeS3Client()
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *args, **kwargs: fake_client))

    object_name = await browser._save_screenshot(FakePage(), "fetch")

    assert object_name and object_name.startswith("browser-screenshots/")
    assert ("head_bucket", browser.settings.object_storage_bucket) in calls
    create_bucket_kwargs = {"Bucket": browser.settings.object_storage_bucket}
    if browser.settings.object_storage_bucket_location:
        create_bucket_kwargs["CreateBucketConfiguration"] = {
            "LocationConstraint": browser.settings.object_storage_bucket_location
        }
    assert ("create_bucket", create_bucket_kwargs) in calls
    assert ("put_object", browser.settings.object_storage_bucket, object_name, 3, "image/png") in calls


@pytest.mark.asyncio
async def test_browser_search_degrades_truthfully_on_live_timeout(monkeypatch):
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
    monkeypatch.setattr(browser.settings, "browserbase_api_key", "")
    monkeypatch.setattr(browser, "_new_page", fake_new_page)

    result = await browser.browser_connector._search({"query": "data observability market", "max_results": 2})

    # No fabricated rows: a failed live search must return an explicit empty result.
    assert "UNAVAILABLE" in result.summary
    assert result.data["tier"] == "unavailable"
    assert result.data["is_unavailable"] is True
    assert "could not be completed" in result.data["warning"]
    assert result.data["results"] == []
