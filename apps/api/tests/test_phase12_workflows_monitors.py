from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.mark.asyncio
async def test_workflow_runtime_prepares_internal_connector_and_completes_default_step():
    from connectors.framework.adapters import adapter_registry
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.seed import seed_builtin_connectors
    from connectors.framework.worker import ConnectorWorker
    from connectors.framework.workflows import WorkflowRuntime

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    await seed_builtin_connectors(repo)
    runtime = WorkflowRuntime(repo, queue)
    workflow = await runtime.create_workflow(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        name="Default workflow",
        steps=[{"id": "capture", "tool_name": "internal_echo__echo", "arguments": {"message": "capture event"}}],
    )

    run = await runtime.start_run(workflow["id"], tenant_id="default", trigger_source="manual")
    tick = await runtime.tick(run["id"], tenant_id="default")
    worker_result = await ConnectorWorker(repo, adapter_registry(), queue).run_once()

    assert tick["ready_step_ids"] == ["capture"]
    assert worker_result["status"] == "success"
    refreshed = await repo.get_workflow_run(run["id"], tenant_id="default")
    steps = await repo.list_workflow_steps(run["id"], tenant_id="default")
    assert refreshed["status"] == "completed"
    assert steps[0]["status"] == "success"


@pytest.mark.asyncio
async def test_workflow_dispatch_ticks_internal_connector_workflow_to_completion():
    from connectors.framework.adapters import adapter_registry
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.seed import seed_builtin_connectors
    from connectors.framework.worker import ConnectorWorker
    from connectors.framework.workflows import WorkflowRuntime

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    await seed_builtin_connectors(repo)
    runtime = WorkflowRuntime(repo, queue)
    workflow = await runtime.create_workflow(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        name="Webhook workflow",
        steps=[{"id": "capture", "tool_name": "internal_echo__echo", "arguments": {"message": "capture event"}}],
        triggers=[{"trigger_type": "webhook", "source": "webhooks", "event_type": "event.received"}],
    )

    dispatched = await runtime.dispatch_event(
        tenant_id="default",
        source="webhooks",
        event_type="event.received",
        payload={"type": "event.received"},
    )
    worker_result = await ConnectorWorker(repo, adapter_registry(), queue).run_once()

    assert dispatched[0]["workflow_id"] == workflow["id"]
    assert worker_result["status"] == "success"
    refreshed = await repo.get_workflow_run(dispatched[0]["id"], tenant_id="default")
    assert refreshed["status"] == "completed"


@pytest.mark.asyncio
async def test_workflow_recovery_ticks_queued_internal_steps_to_completion():
    from connectors.framework.adapters import adapter_registry
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.seed import seed_builtin_connectors
    from connectors.framework.worker import ConnectorWorker
    from connectors.framework.workflows import WorkflowRuntime

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    await seed_builtin_connectors(repo)
    runtime = WorkflowRuntime(repo, queue)
    workflow = await runtime.create_workflow(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        name="Recover workflow",
        steps=[{"id": "capture", "tool_name": "internal_echo__echo", "arguments": {"message": "recover event"}}],
    )
    run = await runtime.start_run(workflow["id"], tenant_id="default", trigger_source="webhooks")

    recovered = await runtime.recover_interrupted_runs(tenant_id="default")
    worker_result = await ConnectorWorker(repo, adapter_registry(), queue).run_once()

    assert run["id"] in recovered
    assert worker_result["status"] == "success"
    refreshed = await repo.get_workflow_run(run["id"], tenant_id="default")
    assert refreshed["status"] == "completed"


@pytest.mark.asyncio
async def test_workflow_step_terminal_status_is_not_downgraded_by_late_running_update():
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.workflows import WorkflowRuntime

    class _Queue:
        pass

    repo = InMemoryConnectorRepository()
    runtime = WorkflowRuntime(repo, _Queue())
    workflow = await runtime.create_workflow(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        name="Race-safe workflow",
        steps=[{"id": "capture", "tool_name": "internal_echo__echo", "arguments": {"message": "done"}}],
    )
    run = await runtime.start_run(workflow["id"], tenant_id="default")
    await runtime.complete_step(run["id"], "capture", tenant_id="default", status="success", output={"message": "done"})

    late = await repo.update_workflow_step(run["id"], "capture", tenant_id="default", status="running", execution_job_id="late-job")

    assert late["status"] == "success"
    assert late.get("execution_job_id") != "late-job"


@pytest.mark.asyncio
async def test_workflow_runtime_persists_triggers_and_dispatches_matching_event():
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.workflows import WorkflowRuntime

    class _Queue:
        pass

    repo = InMemoryConnectorRepository()
    runtime = WorkflowRuntime(repo, _Queue())
    workflow = await runtime.create_workflow(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        name="Inbound lead triage",
        description="Qualify inbound leads",
        steps=[
            {
                "id": "score",
                "tool_name": "internal_echo__echo",
                "arguments": {"message": "score lead"},
                "conditions": [{"field": "payload.type", "operator": "equals", "value": "lead.created"}],
                "max_attempts": 2,
                "approval_required": False,
            }
        ],
        triggers=[
            {"trigger_type": "webhook", "source": "webhooks", "event_type": "lead.created", "config": {"path": "/leads"}},
            {"trigger_type": "connector", "source": "hubspot", "event_type": "contact.created", "config": {"object": "contact"}},
        ],
    )

    triggers = await repo.list_workflow_triggers(workflow["id"], tenant_id="default")
    assert {trigger["trigger_type"] for trigger in triggers} == {"webhook", "connector"}

    dispatched = await runtime.dispatch_event(
        tenant_id="default",
        source="webhooks",
        event_type="lead.created",
        payload={"type": "lead.created", "lead_id": "lead-1"},
    )

    assert len(dispatched) == 1
    assert dispatched[0]["workflow_id"] == workflow["id"]
    runs = await repo.list_workflow_runs(tenant_id="default")
    assert runs[0]["trigger_source"] == "webhooks"
    assert runs[0]["trigger_event_type"] == "lead.created"


@pytest.mark.asyncio
async def test_workflow_pause_resume_recovery_and_run_history():
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.workflows import WorkflowRuntime

    class _Queue:
        pass

    repo = InMemoryConnectorRepository()
    runtime = WorkflowRuntime(repo, _Queue())
    workflow = await runtime.create_workflow(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        name="Digest workflow",
        steps=[{"id": "digest", "tool_name": "internal_echo__echo", "arguments": {"message": "digest"}}],
    )
    run = await runtime.start_run(workflow["id"], tenant_id="default", trigger_source="manual")

    paused = await runtime.pause_run(run["id"], tenant_id="default", reason="operator pause")
    assert paused["status"] == "paused"
    resumed = await runtime.resume_run(run["id"], tenant_id="default")
    assert resumed["status"] == "running"
    recovered = await runtime.recover_interrupted_runs(tenant_id="default")
    assert run["id"] in recovered

    history = await repo.list_workflow_run_history(run["id"], tenant_id="default")
    assert [item["event_type"] for item in history] == ["run_started", "run_paused", "run_resumed", "run_recovered"]


@pytest.mark.asyncio
async def test_monitor_evaluation_creates_cited_alert_and_run_history():
    from jobs import scheduled_tasks as st

    monitor = {
        "id": "mon-1",
        "organization_id": "default",
        "name": "Pricing page",
        "monitor_type": "website",
        "target": "https://example.com/pricing",
        "condition": {"operator": "changed"},
        "last_evidence": {"hash": "old"},
        "status": "active",
    }
    observed = {
        "hash": "new",
        "title": "Pricing",
        "url": "https://example.com/pricing",
        "snippet": "Enterprise price changed",
        "observed_at": datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc).isoformat(),
    }

    alert = st.evaluate_monitor_result(monitor, observed)

    assert alert is not None
    assert alert["monitor_id"] == "mon-1"
    assert alert["severity"] == "info"
    assert alert["evidence"]["url"] == "https://example.com/pricing"
    assert "Enterprise price changed" in alert["summary"]
