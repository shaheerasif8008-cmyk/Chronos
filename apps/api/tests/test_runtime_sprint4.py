import pytest
import os
import json


os.environ["DATABASE_URL"] = "postgresql+asyncpg://chronos:chronos@localhost:5432/chronos"


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
    monkeypatch.setattr(browser, "_new_page", fake_new_page)

    result = await browser.browser_connector._search({"query": "data observability market", "max_results": 2})

    assert result.summary == "Browser search fallback 'data observability market': 2 fixture results"
    assert result.data["tier"] == "fixture"
    assert len(result.data["results"]) == 2
