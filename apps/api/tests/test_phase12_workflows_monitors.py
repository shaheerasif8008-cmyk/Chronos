from __future__ import annotations

from datetime import datetime, timezone

import pytest


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
