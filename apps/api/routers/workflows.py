from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from connectors.framework.adapters import adapter_registry
from connectors.framework.queue_factory import connector_execution_queue
from connectors.framework.repository import DatabaseConnectorRepository
from connectors.framework.seed import seed_builtin_connectors
from connectors.framework.worker import ConnectorWorker
from connectors.framework.workflows import WorkflowRuntime
from core import permissions
from core.auth import get_current_member
from core.config import settings
from core.models import Member

router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowCreateRequest(BaseModel):
    name: str
    description: str = ""
    workspace_id: str = "default"
    employee_id: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    triggers: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowRunRequest(BaseModel):
    workflow_id: str
    trigger_source: str = "manual"
    trigger_event_type: str | None = None
    trigger_payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowTriggerRequest(BaseModel):
    workflow_id: str
    trigger_type: str = Field(pattern="^(schedule|webhook|connector)$")
    source: str
    event_type: str
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowEventDispatchRequest(BaseModel):
    source: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowStepCompleteRequest(BaseModel):
    status: str = Field(pattern="^(success|failed|skipped)$")
    output: dict[str, Any] | None = None
    error: str | None = None


class WorkflowInterventionRequest(BaseModel):
    reason: str | None = None


async def repository() -> DatabaseConnectorRepository:
    repo = DatabaseConnectorRepository()
    await seed_builtin_connectors(repo, tenant_id=settings.org_id)
    return repo


def runtime(repo: DatabaseConnectorRepository) -> WorkflowRuntime:
    return WorkflowRuntime(repo, connector_execution_queue())


async def _drain_ready_workflow_steps(repo: DatabaseConnectorRepository, run_id: str, tenant_id: str) -> None:
    """Run immediately-ready connector jobs so manual UI runs visibly advance."""
    queue = connector_execution_queue()
    worker = ConnectorWorker(repo, adapter_registry(), queue)
    for _ in range(10):
        result = await worker.run_once()
        if not result:
            break
        run = await repo.get_workflow_run(run_id, tenant_id=tenant_id)
        if not run or run.get("status") in {"completed", "failed", "cancelled", "paused", "waiting_for_approval"}:
            break


@router.get("/")
async def list_workflows(status: str | None = Query(default=None), member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_workflows", member.organization_id)
    repo = await repository()
    return await repo.list_workflows(tenant_id=member.organization_id, status=status)


@router.post("/")
async def create_workflow(req: WorkflowCreateRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "create_workflow", req.workspace_id)
    if not req.steps:
        raise HTTPException(status_code=400, detail="Workflow requires at least one step")
    for index, step in enumerate(req.steps):
        if not step.get("id"):
            raise HTTPException(status_code=400, detail=f"Step at index {index} is missing 'id'")
        if not step.get("tool_name"):
            raise HTTPException(
                status_code=400,
                detail=f"Step '{step.get('id', index)}' is missing 'tool_name' (format: connector_id__action_name)",
            )
    repo = await repository()
    return await runtime(repo).create_workflow(
        tenant_id=member.organization_id,
        workspace_id=req.workspace_id,
        employee_id=req.employee_id or member.id,
        user_id=member.id,
        name=req.name,
        description=req.description,
        steps=req.steps,
        triggers=req.triggers,
    )


@router.get("/runs")
async def list_workflow_runs(status: str | None = Query(default=None), member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_workflow_runs", member.organization_id)
    repo = await repository()
    return await repo.list_workflow_runs(tenant_id=member.organization_id, status=status)


@router.post("/runs")
async def start_workflow_run(req: WorkflowRunRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "start_workflow_run", req.workflow_id)
    repo = await repository()
    wf_runtime = runtime(repo)
    run = await wf_runtime.start_run(
        req.workflow_id,
        tenant_id=member.organization_id,
        trigger_source=req.trigger_source,
        trigger_event_type=req.trigger_event_type,
        trigger_payload=req.trigger_payload,
    )
    await wf_runtime.tick(run["id"], tenant_id=member.organization_id)
    await _drain_ready_workflow_steps(repo, run["id"], member.organization_id)
    return await repo.get_workflow_run(run["id"], tenant_id=member.organization_id) or run


@router.get("/triggers")
async def list_workflow_triggers(
    workflow_id: str | None = Query(default=None),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_workflow_triggers", member.organization_id)
    repo = await repository()
    return await repo.list_workflow_triggers(workflow_id, tenant_id=member.organization_id)


@router.post("/triggers")
async def create_workflow_trigger(req: WorkflowTriggerRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "create_workflow_trigger", req.workflow_id)
    repo = await repository()
    workflow = await repo.get_workflow(req.workflow_id, tenant_id=member.organization_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    config = {**req.config, "source": req.source, "event_type": req.event_type}
    return await repo.create_workflow_trigger(
        tenant_id=member.organization_id,
        workflow_id=req.workflow_id,
        trigger_type=req.trigger_type,
        config=config,
        status="active",
    )


@router.post("/dispatch")
async def dispatch_workflow_event(req: WorkflowEventDispatchRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "dispatch_workflow_event", member.organization_id)
    repo = await repository()
    runs = await runtime(repo).dispatch_event(
        tenant_id=member.organization_id,
        source=req.source,
        event_type=req.event_type,
        payload=req.payload,
    )
    return {"dispatched_run_ids": [run["id"] for run in runs], "count": len(runs)}


@router.get("/runs/{run_id}")
async def get_workflow_run(run_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "get_workflow_run", run_id)
    repo = await repository()
    run = await repo.get_workflow_run(run_id, tenant_id=member.organization_id)
    if not run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    return {
        "run": run,
        "steps": await repo.list_workflow_steps(run_id, tenant_id=member.organization_id),
        "dependencies": await repo.list_workflow_dependencies(run_id, tenant_id=member.organization_id),
        "state": await repo.get_workflow_state(run_id, tenant_id=member.organization_id),
        "history": await repo.list_workflow_run_history(run_id, tenant_id=member.organization_id),
    }


@router.post("/runs/{run_id}/tick")
async def tick_workflow_run(run_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "tick_workflow_run", run_id)
    repo = await repository()
    return await runtime(repo).tick(run_id, tenant_id=member.organization_id)


@router.post("/runs/{run_id}/pause")
async def pause_workflow_run(run_id: str, req: WorkflowInterventionRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "pause_workflow_run", run_id)
    repo = await repository()
    return await runtime(repo).pause_run(run_id, tenant_id=member.organization_id, reason=req.reason or "operator pause")


@router.post("/runs/{run_id}/resume")
async def resume_workflow_run(run_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "resume_workflow_run", run_id)
    repo = await repository()
    run = await runtime(repo).resume_run(run_id, tenant_id=member.organization_id)
    await runtime(repo).tick(run_id, tenant_id=member.organization_id)
    return run


@router.post("/runs/{run_id}/cancel")
async def cancel_workflow_run(run_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "cancel_workflow_run", run_id)
    repo = await repository()
    return await runtime(repo).cancel_run(run_id, tenant_id=member.organization_id)


@router.post("/runs/{run_id}/steps/{step_id}/complete")
async def complete_workflow_step(run_id: str, step_id: str, req: WorkflowStepCompleteRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "complete_workflow_step", run_id)
    repo = await repository()
    state = await runtime(repo).complete_step(run_id, step_id, tenant_id=member.organization_id, status=req.status, output=req.output, error=req.error)
    await runtime(repo).tick(run_id, tenant_id=member.organization_id)
    return state


@router.post("/recover")
async def recover_workflows(member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "recover_workflows", member.organization_id)
    repo = await repository()
    return {"recovered_run_ids": await runtime(repo).recover_interrupted_runs(tenant_id=member.organization_id)}
