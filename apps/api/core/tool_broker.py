import hashlib
import json

from core import audit, permissions
from core.models import AgentContext, ToolResult


class ToolBroker:
    async def execute(self, agent: AgentContext, tool: str, args: dict) -> ToolResult:
        args_hash = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
        await permissions.check(agent.as_member(), f"use_tool:{tool}", agent.workspace_id or "default")
        await audit.log(
            "tool_call",
            agent.id,
            tool,
            payload={"args_hash": args_hash},
        )
        result = ToolResult(data={"tool": tool, "args_hash": args_hash}, summary="Phase 1 stub executed")
        await audit.log(
            "tool_result",
            agent.id,
            tool,
            payload={"summary": result.summary},
        )
        return result


tool_broker = ToolBroker()


async def execute(agent: AgentContext, tool: str, args: dict) -> ToolResult:
    return await tool_broker.execute(agent, tool, args)
