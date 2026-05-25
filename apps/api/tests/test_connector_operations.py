import asyncio

import pytest

from core.models import AgentContext


@pytest.mark.asyncio
async def test_policy_engine_denies_time_restricted_destructive_action():
    from connectors.framework.policy import ConnectorPolicy, PolicyEngine

    engine = PolicyEngine(
        [
            ConnectorPolicy(
                id="deny-after-hours",
                decision="deny",
                risk_level="destructive",
                conditions={"business_hours_only": True},
                priority=100,
            )
        ],
        clock=lambda: "23:15",
    )

    decision = await engine.evaluate(
        {
            "tenant_id": "default",
            "workspace_id": "default",
            "employee_id": "employee-1",
            "user_id": "member-1",
            "roles": ["employee"],
        },
        {"id": "danger"},
        {"name": "delete", "risk_level": "destructive"},
        {"allowed_scopes": ["danger.delete"]},
    )

    assert decision.decision == "deny"
    assert "business hours" in decision.reason


@pytest.mark.asyncio
async def test_repository_policy_rules_are_loaded_into_runtime_evaluation():
    from connectors.framework.policy import PolicyEngine
    from connectors.framework.repository import InMemoryConnectorRepository

    repo = InMemoryConnectorRepository()
    await repo.create_policy(
        tenant_id="default",
        workspace_id="default",
        connector_id="internal_echo",
        action_name="echo",
        risk_level="read",
        decision="deny",
        approval_mode="single",
        conditions={},
        priority=50,
        enabled=True,
    )

    policies = await repo.list_policies(tenant_id="default")
    engine = await PolicyEngine.from_repository(repo, tenant_id="default")
    decision = await engine.evaluate(
        {"tenant_id": "default", "workspace_id": "default", "employee_id": "employee-1"},
        {"id": "internal_echo"},
        {"name": "echo", "risk_level": "read"},
        {"allowed_scopes": ["internal.echo"]},
    )

    assert policies[0]["decision"] == "deny"
    assert decision.decision == "deny"
    assert decision.policy_id == policies[0]["id"]


@pytest.mark.asyncio
async def test_write_action_creates_pending_approval_instead_of_executing():
    from connectors.framework.adapters import adapter_registry
    from connectors.framework.approvals import ApprovalService
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.runtime import ConnectorExecutionService
    from connectors.framework.seed import seed_builtin_connectors

    repo = InMemoryConnectorRepository()
    await seed_builtin_connectors(repo)
    connector = await repo.install_connector("internal_echo", tenant_id="default", workspace_id="default")
    repo.actions[(connector["id"], "echo")]["risk_level"] = "external_message"
    await repo.grant_permission(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        connector_id=connector["id"],
        action_name="echo",
        allowed_scopes=["internal.echo"],
        approval_required=False,
    )

    result = await ConnectorExecutionService(repo, adapter_registry(), approval_service=ApprovalService(repo)).execute(
        connector_id=connector["id"],
        action_name="echo",
        arguments={"message": "send this"},
        context=AgentContext(id="employee-1", org_id="default", member_id="member-1", workspace_id="default"),
    )

    assert result.status == "approval_required"
    approvals = await repo.list_approval_requests(tenant_id="default", status="pending")
    assert len(approvals) == 1
    assert approvals[0]["connector_id"] == "internal_echo"
    assert approvals[0]["arguments_redacted"] == {"message": "send this"}


@pytest.mark.asyncio
async def test_approved_request_resumes_execution_through_worker_queue():
    from connectors.framework.adapters import adapter_registry
    from connectors.framework.approvals import ApprovalService
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.queued_runtime import QueuedConnectorExecutionService
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.seed import seed_builtin_connectors
    from connectors.framework.worker import ConnectorWorker

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    await seed_builtin_connectors(repo)
    connector = await repo.install_connector("internal_echo", tenant_id="default", workspace_id="default")
    repo.actions[(connector["id"], "echo")]["risk_level"] = "external_message"
    await repo.grant_permission(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        connector_id=connector["id"],
        action_name="echo",
        allowed_scopes=["internal.echo"],
        approval_required=False,
    )

    initial = await QueuedConnectorExecutionService(repo, queue).enqueue(
        connector_id=connector["id"],
        action_name="echo",
        arguments={"message": "approved later"},
        context=AgentContext(id="employee-1", org_id="default", member_id="member-1", workspace_id="default"),
    )
    approval_id = initial.output["approval_request_id"]

    resumed = await ApprovalService(repo).approve_and_enqueue(
        approval_id,
        tenant_id="default",
        actor_id="admin-1",
        queue=queue,
    )
    worker_result = await ConnectorWorker(repo, adapter_registry(), queue).run_once()

    assert resumed["status"] == "queued"
    assert worker_result["status"] == "success"
    assert worker_result["result"] == {"message": "approved later"}
    approvals = await repo.list_approval_requests(tenant_id="default", status="approved")
    assert approvals[0]["id"] == approval_id


@pytest.mark.asyncio
async def test_worker_queue_retries_timeout_and_records_trace():
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.tracing import ExecutionTracer
    from connectors.framework.worker import ConnectorWorker
    from connectors.framework.models import ConnectorResult

    class SlowAdapter:
        async def validate_credentials(self, credentials):
            return True

        async def execute(self, action_name, args, context):
            await asyncio.sleep(0.05)
            return ConnectorResult(status="success", output={"ok": True})

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    tracer = ExecutionTracer(repo)
    await queue.enqueue(
        {
            "id": "job-1",
            "tenant_id": "default",
            "workspace_id": "default",
            "employee_id": "employee-1",
            "user_id": "member-1",
            "connector_id": "slow",
            "action_name": "wait",
            "arguments": {},
            "max_attempts": 2,
            "timeout_ms": 1,
        }
    )

    worker = ConnectorWorker(repo, {"slow": SlowAdapter()}, queue, tracer=tracer)
    job = await worker.run_once()

    assert job["status"] == "timeout"
    assert job["attempts"] == 2
    traces = await repo.list_execution_traces(tenant_id="default")
    assert traces[0]["status"] == "timeout"
    steps = await repo.list_trace_steps(traces[0]["id"])
    assert [step["status"] for step in steps] == ["timeout", "timeout"]


@pytest.mark.asyncio
async def test_cancelled_queued_job_is_not_executed_by_worker():
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.worker import ConnectorWorker
    from connectors.framework.models import ConnectorResult

    class CountingAdapter:
        def __init__(self):
            self.calls = 0

        async def validate_credentials(self, credentials):
            return True

        async def execute(self, action_name, args, context):
            self.calls += 1
            return ConnectorResult(status="success", output={"called": True})

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    job = await repo.create_execution_job(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        connector_id="internal_echo",
        action_name="echo",
        arguments={"message": "do not run"},
    )
    await queue.enqueue(
        {
            "id": job["id"],
            "tenant_id": "default",
            "workspace_id": "default",
            "employee_id": "employee-1",
            "user_id": "member-1",
            "connector_id": "internal_echo",
            "action_name": "echo",
            "arguments": {"message": "do not run"},
        }
    )
    await repo.cancel_execution_job(job["id"], tenant_id="default")

    adapter = CountingAdapter()
    result = await ConnectorWorker(repo, {"internal_echo": adapter}, queue).run_once()

    assert result["status"] == "cancelled"
    assert adapter.calls == 0
    stored = await repo.get_execution_job(job["id"], tenant_id="default")
    assert stored["status"] == "cancelled"


@pytest.mark.asyncio
async def test_planner_only_uses_registered_permitted_tools():
    from connectors.framework.planner import ToolOrchestrationPlanner
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.seed import seed_builtin_connectors

    repo = InMemoryConnectorRepository()
    await seed_builtin_connectors(repo)
    await repo.install_connector("internal_time", tenant_id="default", workspace_id="default")
    await repo.grant_permission(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        connector_id="internal_time",
        action_name="now",
        allowed_scopes=["internal.time"],
        approval_required=False,
    )

    plan = await ToolOrchestrationPlanner(repo).create_plan(
        "Get the server time",
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
    )

    assert [step.tool_name for step in plan.steps] == ["internal_time__now"]
    assert plan.steps[0].dependencies == []


@pytest.mark.asyncio
async def test_browser_connector_is_registered_and_executable(monkeypatch):
    from connectors import browser as browser_module
    from connectors.framework.adapters import adapter_registry
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.runtime import ConnectorExecutionService
    from connectors.framework.seed import seed_builtin_connectors
    from core.models import ToolResult

    async def fake_execute(tool, args):
        return ToolResult(data={"url": args["url"], "content": "example content"}, summary=f"Fetched {args['url']}: 15 chars")

    monkeypatch.setattr(browser_module.browser_connector, "execute", fake_execute)

    repo = InMemoryConnectorRepository()
    await seed_builtin_connectors(repo)
    connector = await repo.install_connector("browser", tenant_id="default", workspace_id="default")
    await repo.grant_permission(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        connector_id=connector["id"],
        action_name="fetch",
        allowed_scopes=["browser.fetch"],
        approval_required=False,
    )

    tools = await repo.list_permitted_actions(tenant_id="default", workspace_id="default", employee_id="employee-1")
    assert [action["name"] for _, action, _ in tools] == ["fetch"]

    result = await ConnectorExecutionService(repo, adapter_registry()).execute(
        connector_id="browser",
        action_name="fetch",
        arguments={"url": "https://example.com"},
        context=AgentContext(id="employee-1", org_id="default", member_id="member-1", workspace_id="default"),
    )

    assert result.status == "success"
    assert result.output == {
        "summary": "Fetched https://example.com: 15 chars",
        "data": {"url": "https://example.com", "content": "example content"},
    }


@pytest.mark.asyncio
async def test_planner_execution_stops_at_approval_checkpoint():
    from connectors.framework.planner import ToolExecutionPlan, ToolExecutionStep, ToolOrchestrationPlanner
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.seed import seed_builtin_connectors

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    await seed_builtin_connectors(repo)
    await repo.install_connector("internal_echo", tenant_id="default", workspace_id="default")
    await repo.grant_permission(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        connector_id="internal_echo",
        action_name="echo",
        allowed_scopes=["internal.echo"],
        approval_required=True,
    )
    plan = ToolExecutionPlan(
        id="plan-approval",
        goal="Send a message",
        steps=[
            ToolExecutionStep(
                id="step-1",
                tool_name="internal_echo__echo",
                arguments={"message": "needs approval"},
            )
        ],
    )

    result = await ToolOrchestrationPlanner(repo).execute_plan(
        plan,
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        queue=queue,
    )

    assert result["status"] == "approval_required"
    assert result["steps"][0]["status"] == "approval_required"
    assert result["steps"][0]["approval_request_id"]


@pytest.mark.asyncio
async def test_mcp_discovery_records_transport_error_truthfully():
    from connectors.framework.mcp import MCPDiscoveryService
    from connectors.framework.repository import InMemoryConnectorRepository

    repo = InMemoryConnectorRepository()
    server = await repo.register_mcp_server(
        tenant_id="default",
        name="Local MCP",
        transport="local",
        command="missing-mcp-server",
    )

    result = await MCPDiscoveryService(repo).discover(server["id"], tenant_id="default")

    assert result["status"] == "error"
    assert result["message"]
    logs = await repo.list_mcp_discovery_logs(tenant_id="default")
    assert logs[0]["server_id"] == server["id"]


def test_context_compression_chunks_large_outputs_and_preserves_metadata():
    from connectors.framework.compression import compress_connector_output

    compressed = compress_connector_output(
        {"items": [{"id": i, "body": "alpha beta gamma " * 40} for i in range(12)]},
        metadata={"connector_id": "internal_echo", "action_name": "echo"},
        max_tokens=80,
        query="beta",
    )

    assert compressed["token_estimate"] <= 80
    assert compressed["metadata"]["connector_id"] == "internal_echo"
    assert compressed["references"]
    assert compressed["truncated"] is True


@pytest.mark.asyncio
async def test_health_service_marks_connector_degraded_after_failures():
    from connectors.framework.health import ConnectorHealthService
    from connectors.framework.repository import InMemoryConnectorRepository

    repo = InMemoryConnectorRepository()
    service = ConnectorHealthService(repo)
    await service.record_execution("default", "internal_echo", "success", duration_ms=20)
    await service.record_execution("default", "internal_echo", "failure", duration_ms=200)
    await service.record_execution("default", "internal_echo", "timeout", duration_ms=1000)

    health = await repo.get_connector_health(tenant_id="default", connector_id="internal_echo")

    assert health["status"] == "degraded"
    assert health["failure_rate"] > 0
    assert health["timeout_rate"] > 0
