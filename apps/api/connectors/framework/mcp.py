from __future__ import annotations

from typing import Any

from connectors.mcp_client import MCPTransportError, call_mcp


class MCPDiscoveryService:
    def __init__(self, repo: Any) -> None:
        self.repo = repo

    async def discover(self, server_id: str, *, tenant_id: str) -> dict[str, Any]:
        server = await self.repo.get_mcp_server(server_id, tenant_id=tenant_id)
        if not server:
            result = {"status": "error", "message": "MCP server not found", "tools_discovered": 0}
        else:
            try:
                discovered = await call_mcp(server, "tools/list", {})
                tools = discovered.get("tools") or []
                result = {"status": "healthy", "message": f"Discovered {len(tools)} MCP tools", "tools_discovered": len(tools), "tools": tools}
            except (MCPTransportError, OSError, TimeoutError, ValueError) as exc:
                result = {"status": "error", "message": str(exc), "tools_discovered": 0}
        await self.repo.log_mcp_discovery(
            tenant_id=tenant_id,
            server_id=server_id,
            status=result["status"],
            message=result["message"],
            tools_discovered=result["tools_discovered"],
        )
        return result
