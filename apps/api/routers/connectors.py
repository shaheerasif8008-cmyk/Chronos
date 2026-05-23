from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from connectors.framework.adapters import adapter_registry
from connectors.framework.approvals import ApprovalService
from connectors.framework.mcp import MCPDiscoveryService
from connectors.framework.planner import ToolOrchestrationPlanner
from connectors.framework.queue_factory import connector_execution_queue
from connectors.framework.queued_runtime import QueuedConnectorExecutionService
from connectors.framework.repository import DatabaseConnectorRepository
from connectors.framework.runtime import ConnectorExecutionService
from connectors.framework.seed import seed_builtin_connectors
from connectors.framework.tool_calling import execute_tool_call, get_available_tools_for_employee
from core import permissions
from core.auth import get_current_member
from core.config import settings
from core.models import AgentContext, Member

router = APIRouter(prefix="/connectors", tags=["connectors"])


class InstallConnectorRequest(BaseModel):
    workspace_id: str = "default"


class PermissionRequest(BaseModel):
    workspace_id: str = "default"
    employee_id: str
    user_id: str | None = None
    action_name: str
    allowed_scopes: list[str] = Field(default_factory=list)
    approval_required: bool = False


class ExecuteConnectorRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    workspace_id: str = "default"
    employee_id: str | None = None


class ToolCallRequest(BaseModel):
    tool_call: dict[str, Any]
    workspace_id: str = "default"
    employee_id: str | None = None


class ConnectorProofRequest(BaseModel):
    message: str = "Chronos connector proof"


class ResolveApprovalRequest(BaseModel):
    approved: bool
    note: str | None = None


class RegisterMCPServerRequest(BaseModel):
    name: str
    transport: str = Field(pattern="^(local|remote)$")
    command: str | None = None
    server_url: str | None = None


class PlanRequest(BaseModel):
    goal: str
    workspace_id: str = "default"
    employee_id: str | None = None


class ExecutePlanRequest(BaseModel):
    plan: dict[str, Any]
    workspace_id: str = "default"
    employee_id: str | None = None


class PolicyRequest(BaseModel):
    workspace_id: str | None = None
    employee_id: str | None = None
    role: str | None = None
    connector_id: str | None = None
    action_name: str | None = None
    risk_level: str | None = None
    decision: str = Field(pattern="^(allow|deny|require_approval)$")
    approval_mode: str = Field(default="single", pattern="^(single|admin|multi)$")
    conditions: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    enabled: bool = True


def repo() -> DatabaseConnectorRepository:
    return DatabaseConnectorRepository()


async def ensure_registry() -> DatabaseConnectorRepository:
    repository = repo()
    await seed_builtin_connectors(repository, tenant_id=settings.org_id)
    return repository


def clean_connector(row: dict[str, Any]) -> dict[str, Any]:
    category = "Internal" if row.get("provider") == "internal" else "Productivity"
    return {
        "id": row["id"],
        "name": {"internal_echo": "Runtime Diagnostics", "internal_time": "System Clock"}.get(row["id"], row.get("name") or row["id"]),
        "provider": row.get("provider"),
        "description": row.get("description") or "",
        "type": row.get("type") or "native",
        "category": category,
        "status": row.get("status") or "available",
        "auth_type": row.get("auth_type") or "none",
        "scopes": row.get("scopes") or [],
        "actions": row.get("actions") or [],
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
        "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
    }


def clean_action(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["name"],
        "description": row["description"],
        "parameters_schema": row["parameters_schema"],
        "output_schema": row.get("output_schema"),
        "required_permissions": row.get("required_permissions") or [],
        "risk_level": row["risk_level"],
        "approval_required": bool(row.get("approval_required")),
    }


@router.get("/")
async def list_connectors(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connectors", member.organization_id)
    repository = await ensure_registry()
    rows = await repository.list_connectors(tenant_id=member.organization_id)
    return [clean_connector(row) for row in rows if row.get("actions")]


@router.get("/execution-logs")
async def list_connector_execution_logs(
    connector_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_execution_logs", member.organization_id)
    repository = await ensure_registry()
    rows = await repository.list_execution_logs(tenant_id=member.organization_id, connector_id=connector_id, limit=limit)
    return [
        {
            **dict(row),
            "created_at": str(row["created_at"]) if row.get("created_at") else None,
        }
        for row in rows
    ]


@router.get("/approvals")
async def list_connector_approvals(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_approvals", member.organization_id)
    repository = await ensure_registry()
    rows = await repository.list_approval_requests(tenant_id=member.organization_id, status=status, limit=limit)
    return [{**dict(row), "created_at": str(row.get("created_at")) if row.get("created_at") else None, "resolved_at": str(row.get("resolved_at")) if row.get("resolved_at") else None} for row in rows]


@router.post("/approvals/{approval_id}/resolve")
async def resolve_connector_approval(approval_id: str, req: ResolveApprovalRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "resolve_connector_approval", approval_id)
    repository = await ensure_registry()
    try:
        if req.approved:
            return await ApprovalService(repository).approve_and_enqueue(
                approval_id,
                tenant_id=member.organization_id,
                actor_id=member.id,
                queue=connector_execution_queue(),
                note=req.note,
            )
        row = await ApprovalService(repository).resolve(
            approval_id,
            tenant_id=member.organization_id,
            actor_id=member.id,
            approved=False,
            note=req.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return dict(row)


@router.get("/health")
async def list_connector_health(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_health", member.organization_id)
    repository = await ensure_registry()
    rows = await repository.list_connector_health(tenant_id=member.organization_id)
    return [{**dict(row), "updated_at": str(row.get("updated_at")) if row.get("updated_at") else None} for row in rows]


@router.get("/execution-traces")
async def list_connector_execution_traces(
    limit: int = Query(default=50, ge=1, le=100),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_execution_traces", member.organization_id)
    repository = await ensure_registry()
    rows = await repository.list_execution_traces(tenant_id=member.organization_id, limit=limit)
    return [{**dict(row), "started_at": str(row.get("started_at")) if row.get("started_at") else None, "completed_at": str(row.get("completed_at")) if row.get("completed_at") else None} for row in rows]


@router.get("/execution-jobs")
async def list_connector_execution_jobs(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_execution_jobs", member.organization_id)
    repository = await ensure_registry()
    rows = await repository.list_execution_jobs(tenant_id=member.organization_id, status=status, limit=limit)
    return [{**dict(row), "created_at": str(row.get("created_at")) if row.get("created_at") else None, "updated_at": str(row.get("updated_at")) if row.get("updated_at") else None} for row in rows]


@router.post("/execution-jobs/{job_id}/cancel")
async def cancel_connector_execution_job(job_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "cancel_connector_execution_job", job_id)
    repository = await ensure_registry()
    try:
        return await repository.cancel_execution_job(job_id, tenant_id=member.organization_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/execution-traces/{trace_id}")
async def get_connector_execution_trace(trace_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "get_connector_execution_trace", trace_id)
    repository = await ensure_registry()
    traces = await repository.list_execution_traces(tenant_id=member.organization_id, limit=100)
    trace = next((row for row in traces if row["id"] == trace_id), None)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"trace": dict(trace), "steps": await repository.list_trace_steps(trace_id)}


@router.post("/plans")
async def create_connector_plan(req: PlanRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "create_connector_plan", req.workspace_id)
    repository = await ensure_registry()
    plan = await ToolOrchestrationPlanner(repository).create_plan(
        req.goal,
        tenant_id=member.organization_id,
        workspace_id=req.workspace_id,
        employee_id=req.employee_id or member.id,
    )
    return {"id": plan.id, "goal": plan.goal, "steps": [step.__dict__ for step in plan.steps]}


@router.post("/plans/execute")
async def execute_connector_plan(req: ExecutePlanRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    from connectors.framework.planner import ToolExecutionPlan, ToolExecutionStep

    await permissions.check(member, "execute_connector_plan", req.workspace_id)
    repository = await ensure_registry()
    raw_steps = req.plan.get("steps") or []
    plan = ToolExecutionPlan(
        id=req.plan.get("id") or "ad_hoc_plan",
        goal=req.plan.get("goal") or "",
        steps=[
            ToolExecutionStep(
                id=step.get("id") or f"step-{index + 1}",
                tool_name=step["tool_name"],
                arguments=step.get("arguments") or {},
                dependencies=step.get("dependencies") or [],
                approval_required=bool(step.get("approval_required")),
                parallel_safe=bool(step.get("parallel_safe")),
            )
            for index, step in enumerate(raw_steps)
        ],
    )
    return await ToolOrchestrationPlanner(repository).execute_plan(
        plan,
        tenant_id=member.organization_id,
        workspace_id=req.workspace_id,
        employee_id=req.employee_id or member.id,
        user_id=member.id,
        queue=connector_execution_queue(),
    )


@router.get("/mcp")
async def list_mcp_servers(member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "list_mcp_servers", member.organization_id)
    repository = await ensure_registry()
    return {
        "servers": await repository.list_mcp_servers(tenant_id=member.organization_id),
        "discovery_logs": await repository.list_mcp_discovery_logs(tenant_id=member.organization_id),
    }


@router.post("/mcp/register")
async def register_mcp_server(req: RegisterMCPServerRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "register_mcp_server", member.organization_id)
    if req.transport == "local" and not req.command:
        raise HTTPException(status_code=400, detail="Local MCP servers require a command")
    if req.transport == "remote" and not req.server_url:
        raise HTTPException(status_code=400, detail="Remote MCP servers require a server_url")
    repository = await ensure_registry()
    return await repository.register_mcp_server(
        tenant_id=member.organization_id,
        name=req.name,
        transport=req.transport,
        command=req.command,
        server_url=req.server_url,
    )


@router.post("/mcp/{server_id}/discover")
async def discover_mcp_server(server_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "discover_mcp_server", server_id)
    repository = await ensure_registry()
    return await MCPDiscoveryService(repository).discover(server_id, tenant_id=member.organization_id)


@router.get("/policies")
async def list_connector_policies(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_policies", member.organization_id)
    repository = await ensure_registry()
    return await repository.list_policies(tenant_id=member.organization_id)


@router.post("/policies")
async def create_connector_policy(req: PolicyRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "create_connector_policy", member.organization_id)
    repository = await ensure_registry()
    return await repository.create_policy(
        tenant_id=member.organization_id,
        workspace_id=req.workspace_id,
        employee_id=req.employee_id,
        role=req.role,
        connector_id=req.connector_id,
        action_name=req.action_name,
        risk_level=req.risk_level,
        decision=req.decision,
        approval_mode=req.approval_mode,
        conditions=req.conditions,
        priority=req.priority,
        enabled=req.enabled,
    )


@router.delete("/policies/{policy_id}")
async def delete_connector_policy(policy_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "delete_connector_policy", policy_id)
    repository = await ensure_registry()
    await repository.delete_policy(policy_id, tenant_id=member.organization_id)
    return {"id": policy_id, "deleted": True}


@router.get("/tools")
async def list_available_tools(
    employee_id: str,
    workspace_id: str = Query(default="default"),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_tools", workspace_id)
    repository = await ensure_registry()
    return await get_available_tools_for_employee(
        repository,
        employee_id=employee_id,
        workspace_id=workspace_id,
        tenant_id=member.organization_id,
    )


@router.post("/tool-call")
async def execute_connector_tool_call(req: ToolCallRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "execute_connector_tool_call", req.workspace_id)
    repository = await ensure_registry()
    return await execute_tool_call(
        repository,
        req.tool_call,
        AgentContext(
            id=req.employee_id or member.id,
            org_id=member.organization_id,
            member_id=member.id,
            workspace_id=req.workspace_id,
        ),
    )


@router.post("/{connector_id}/install")
async def install_connector(connector_id: str, req: InstallConnectorRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "install_connector", connector_id)
    repository = await ensure_registry()
    connector = await repository.get_connector(connector_id, tenant_id=member.organization_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.get("type") == "mcp":
        raise HTTPException(status_code=501, detail="MCP transport is not implemented for production execution")

    installed = await repository.install_connector(
        connector_id,
        tenant_id=member.organization_id,
        workspace_id=req.workspace_id,
        installed_by=member.id,
    )
    for action in await repository.list_actions(connector_id):
        await repository.grant_permission(
            tenant_id=member.organization_id,
            workspace_id=req.workspace_id,
            employee_id=member.id,
            user_id=member.id,
            connector_id=connector_id,
            action_name=action["name"],
            allowed_scopes=action.get("required_permissions") or [],
            approval_required=bool(action.get("approval_required")),
        )
    return clean_connector(installed)


@router.post("/{connector_id}/disable")
async def disable_connector(connector_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "disable_connector", connector_id)
    repository = await ensure_registry()
    if not await repository.get_connector(connector_id, tenant_id=member.organization_id):
        raise HTTPException(status_code=404, detail="Connector not found")
    await repository.disable_connector(connector_id, tenant_id=member.organization_id)
    return {"id": connector_id, "status": "disabled"}


@router.get("/{connector_id}/actions")
async def list_connector_actions(connector_id: str, member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_actions", connector_id)
    repository = await ensure_registry()
    if not await repository.get_connector(connector_id, tenant_id=member.organization_id):
        raise HTTPException(status_code=404, detail="Connector not found")
    return [clean_action(row) for row in await repository.list_actions(connector_id)]


@router.post("/{connector_id}/permissions")
async def grant_connector_permission(connector_id: str, req: PermissionRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "grant_connector_permission", connector_id)
    repository = await ensure_registry()
    action = await repository.get_action(connector_id, req.action_name)
    if not action:
        raise HTTPException(status_code=404, detail="Connector action not found")
    return await repository.grant_permission(
        tenant_id=member.organization_id,
        workspace_id=req.workspace_id,
        employee_id=req.employee_id,
        user_id=req.user_id,
        connector_id=connector_id,
        action_name=req.action_name,
        allowed_scopes=req.allowed_scopes,
        approval_required=req.approval_required,
    )


@router.delete("/{connector_id}/permissions/{action_name}")
async def revoke_connector_permission(
    connector_id: str,
    action_name: str,
    workspace_id: str = Query(default="default"),
    employee_id: str = Query(...),
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "revoke_connector_permission", connector_id)
    repository = await ensure_registry()
    await repository.revoke_permission(
        tenant_id=member.organization_id,
        workspace_id=workspace_id,
        employee_id=employee_id,
        connector_id=connector_id,
        action_name=action_name,
    )
    return {"connector_id": connector_id, "action_name": action_name, "revoked": True}


@router.post("/{connector_id}/actions/{action_name}/execute")
async def execute_connector_action(
    connector_id: str,
    action_name: str,
    req: ExecuteConnectorRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "execute_connector_action", connector_id)
    repository = await ensure_registry()
    result = await QueuedConnectorExecutionService(repository, connector_execution_queue()).enqueue(
        connector_id=connector_id,
        action_name=action_name,
        arguments=req.arguments,
        context=AgentContext(
            id=req.employee_id or member.id,
            org_id=member.organization_id,
            member_id=member.id,
            workspace_id=req.workspace_id,
        ),
    )
    return {
        "status": result.status,
        "output": result.output,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


# Compatibility helper retained for old tests only. It now routes through the
# real internal connector framework instead of claiming Gmail/browser support.
async def execute_connector_proof(
    *,
    connector_id: str,
    provider: str,
    member: Member,
    req: ConnectorProofRequest | None = None,
) -> dict[str, Any]:
    repository = await ensure_registry()
    installed = await repository.install_connector(
        "internal_echo",
        tenant_id=member.organization_id,
        workspace_id="default",
        installed_by=member.id,
    )
    await repository.grant_permission(
        tenant_id=member.organization_id,
        workspace_id="default",
        employee_id=member.id,
        user_id=member.id,
        connector_id=installed["id"],
        action_name="echo",
        allowed_scopes=["internal.echo"],
        approval_required=False,
    )
    result = await ConnectorExecutionService(repository, adapter_registry()).execute(
        connector_id="internal_echo",
        action_name="echo",
        arguments={"message": (req.message if req else "Chronos connector proof")},
        context=AgentContext(id=member.id, org_id=member.organization_id, member_id=member.id, workspace_id="default"),
    )
    return {"status": result.status, "detail": result.output or result.error, "tool": "internal_echo.echo"}
