from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from connectors.framework.queued_runtime import QueuedConnectorExecutionService
from connectors.framework.tool_calling import get_available_tools_for_employee
from core.models import AgentContext


@dataclass(frozen=True)
class ToolExecutionStep:
    id: str
    tool_name: str
    arguments: dict[str, Any]
    dependencies: list[str] = field(default_factory=list)
    approval_required: bool = False
    parallel_safe: bool = False


@dataclass(frozen=True)
class ToolExecutionPlan:
    id: str
    goal: str
    steps: list[ToolExecutionStep]


class ToolOrchestrationPlanner:
    def __init__(self, repo: Any) -> None:
        self.repo = repo

    async def create_plan(
        self,
        goal: str,
        *,
        tenant_id: str,
        workspace_id: str,
        employee_id: str,
    ) -> ToolExecutionPlan:
        tools = await get_available_tools_for_employee(
            self.repo,
            employee_id=employee_id,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
        )
        steps: list[ToolExecutionStep] = []
        lower_goal = goal.lower()
        for tool in tools:
            name = tool["function"]["name"]
            if "time" in lower_goal and "time" in name:
                steps.append(
                    ToolExecutionStep(
                        id=f"step-{len(steps) + 1}",
                        tool_name=name,
                        arguments={},
                        dependencies=[],
                        approval_required=tool["metadata"]["approval_required"],
                        parallel_safe=tool["metadata"]["risk_level"] == "read",
                    )
                )
        if not steps and tools:
            tool = tools[0]
            steps.append(
                ToolExecutionStep(
                    id="step-1",
                    tool_name=tool["function"]["name"],
                    arguments={},
                    dependencies=[],
                    approval_required=tool["metadata"]["approval_required"],
                    parallel_safe=tool["metadata"]["risk_level"] == "read",
                )
            )
        plan = ToolExecutionPlan(id="plan-1", goal=goal, steps=steps)
        if hasattr(self.repo, "create_tool_execution_plan"):
            await self.repo.create_tool_execution_plan(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                employee_id=employee_id,
                goal=goal,
                steps=[step.__dict__ for step in steps],
            )
        return plan

    async def execute_plan(
        self,
        plan: ToolExecutionPlan,
        *,
        tenant_id: str,
        workspace_id: str,
        employee_id: str,
        user_id: str,
        queue: Any,
    ) -> dict[str, Any]:
        available = await get_available_tools_for_employee(
            self.repo,
            employee_id=employee_id,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
        )
        allowed_names = {tool["function"]["name"] for tool in available}
        results: list[dict[str, Any]] = []
        status = "success"
        service = QueuedConnectorExecutionService(self.repo, queue)
        for step in plan.steps:
            if step.tool_name not in allowed_names:
                status = "permission_denied"
                results.append({"id": step.id, "tool_name": step.tool_name, "status": status, "error": "Tool is not available to this employee"})
                break
            connector_id, action_name = step.tool_name.split("__", 1)
            outcome = await service.enqueue(
                connector_id=connector_id,
                action_name=action_name,
                arguments=step.arguments,
                context=AgentContext(id=employee_id, org_id=tenant_id, member_id=user_id, workspace_id=workspace_id),
            )
            step_result = {
                "id": step.id,
                "tool_name": step.tool_name,
                "status": outcome.status,
                "output": outcome.output,
                "error": outcome.error,
                "approval_request_id": (outcome.output or {}).get("approval_request_id"),
                "job_id": (outcome.output or {}).get("job_id"),
            }
            results.append(step_result)
            if outcome.status in {"approval_required", "failure", "timeout", "validation_error", "permission_denied"}:
                status = outcome.status
                break
        return {"id": plan.id, "goal": plan.goal, "status": status, "steps": results}
