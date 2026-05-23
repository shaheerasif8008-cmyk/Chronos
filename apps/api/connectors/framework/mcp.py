from __future__ import annotations

from typing import Any


class MCPDiscoveryService:
    def __init__(self, repo: Any) -> None:
        self.repo = repo

    async def discover(self, server_id: str, *, tenant_id: str) -> dict[str, Any]:
        server = await self.repo.get_mcp_server(server_id, tenant_id=tenant_id)
        if not server:
            result = {"status": "error", "message": "MCP server not found", "tools_discovered": 0}
        else:
            result = {
                "status": "error",
                "message": "MCP transport discovery is registered but execution transport is not implemented",
                "tools_discovered": 0,
            }
        await self.repo.log_mcp_discovery(
            tenant_id=tenant_id,
            server_id=server_id,
            status=result["status"],
            message=result["message"],
            tools_discovered=result["tools_discovered"],
        )
        return result
