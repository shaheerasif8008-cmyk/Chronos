from __future__ import annotations

from typing import Any

from connectors.framework.queue_factory import connector_execution_queue
from connectors.framework.queued_runtime import QueuedConnectorExecutionService
from connectors.framework.repository import ConnectorRepository
from core.models import AgentContext


def tool_name(connector_id: str, action_name: str) -> str:
    return f"{connector_id}__{action_name}"


async def get_available_tools_for_employee(
    repo: ConnectorRepository,
    *,
    employee_id: str,
    workspace_id: str,
    tenant_id: str,
) -> list[dict[str, Any]]:
    rows = await repo.list_permitted_actions(tenant_id=tenant_id, workspace_id=workspace_id, employee_id=employee_id)
    tools = []
    for connector, action, permission in rows:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool_name(connector["id"], action["name"]),
                    "description": action["description"],
                    "parameters": action["parameters_schema"],
                },
                "metadata": {
                    "connector_id": connector["id"],
                    "action_name": action["name"],
                    "risk_level": action["risk_level"],
                    "approval_required": bool(action.get("approval_required")) or bool(permission.get("approval_required")),
                },
            }
        )
    return tools


async def execute_tool_call(
    repo: ConnectorRepository,
    tool_call: dict[str, Any],
    context: AgentContext,
) -> dict[str, Any]:
    function = tool_call.get("function") or {}
    name = function.get("name") or tool_call.get("name")
    if not name or "__" not in name:
        return {"status": "validation_error", "error": "Tool name must be connector_id__action_name"}
    connector_id, action_name = name.split("__", 1)
    arguments = function.get("arguments") or tool_call.get("arguments") or {}
    if not isinstance(arguments, dict):
        return {"status": "validation_error", "error": "Tool arguments must be a JSON object"}

    result = await QueuedConnectorExecutionService(repo, connector_execution_queue()).enqueue(
        connector_id=connector_id,
        action_name=action_name,
        arguments=arguments,
        context=context,
        metadata={"idempotency_key": str(tool_call["idempotency_key"])}
        if tool_call.get("idempotency_key")
        else None,
    )
    return {"status": result.status, "output": result.output, "error": result.error}
