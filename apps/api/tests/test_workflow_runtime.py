import pytest


@pytest.mark.asyncio
async def test_workflow_runtime_schedules_ready_dag_steps_and_checkpoints_state():
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.seed import seed_builtin_connectors
    from connectors.framework.workflows import WorkflowRuntime

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    await seed_builtin_connectors(repo)
    await repo.install_connector("internal_echo", tenant_id="default", workspace_id="default")
    await repo.install_connector("internal_time", tenant_id="default", workspace_id="default")
    for connector_id, action_name, scope in [("internal_echo", "echo", "internal.echo"), ("internal_time", "now", "internal.time")]:
        await repo.grant_permission(
            tenant_id="default",
            workspace_id="default",
            employee_id="employee-1",
            user_id="member-1",
            connector_id=connector_id,
            action_name=action_name,
            allowed_scopes=[scope],
            approval_required=False,
        )

    runtime = WorkflowRuntime(repo, queue)
    workflow = await runtime.create_workflow(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        name="DAG proof",
        steps=[
            {"id": "retrieve", "tool_name": "internal_echo__echo", "arguments": {"message": "lead"}},
            {"id": "timestamp", "tool_name": "internal_time__now", "arguments": {}, "dependencies": ["retrieve"]},
        ],
    )
    run = await runtime.start_run(workflow["id"], tenant_id="default")

    first = await runtime.tick(run["id"], tenant_id="default")
    queued = await queue.dequeue()
    assert first["status"] == "running"
    assert queued["workflow_run_id"] == run["id"]
    assert queued["workflow_step_id"] == "retrieve"

    await runtime.complete_step(run["id"], "retrieve", tenant_id="default", status="success", output={"message": "lead"})
    second = await runtime.tick(run["id"], tenant_id="default")
    queued_second = await queue.dequeue()

    assert second["ready_step_ids"] == ["timestamp"]
    assert queued_second["workflow_step_id"] == "timestamp"
    state = await repo.get_workflow_state(run["id"], tenant_id="default")
    assert state["snapshot"]["steps"]["retrieve"]["status"] == "success"


@pytest.mark.asyncio
async def test_workflow_runtime_pauses_and_recovers_interrupted_runs():
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.workflows import WorkflowRuntime

    repo = InMemoryConnectorRepository()
    runtime = WorkflowRuntime(repo, InMemoryExecutionQueue())
    workflow = await runtime.create_workflow(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        name="Recovery proof",
        steps=[{"id": "a", "tool_name": "internal_echo__echo", "arguments": {"message": "x"}}],
    )
    run = await runtime.start_run(workflow["id"], tenant_id="default")
    await runtime.pause_run(run["id"], tenant_id="default", reason="operator pause")

    paused = await repo.get_workflow_run(run["id"], tenant_id="default")
    assert paused["status"] == "paused"

    await repo.update_workflow_run(run["id"], tenant_id="default", status="running")
    recovered = await runtime.recover_interrupted_runs(tenant_id="default")
    restored = await repo.get_workflow_run(run["id"], tenant_id="default")

    assert recovered == [run["id"]]
    assert restored["status"] == "recovering"
