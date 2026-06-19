from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any

import httpx

from connectors.framework.repository import DatabaseConnectorRepository
from core.models import AgentContext, ToolResult

JSONRPC_VERSION = "2.0"


class MCPTransportError(RuntimeError):
    pass


def _encode_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            raise MCPTransportError("MCP server closed stdout")
        if line in {b"\r\n", b"\n"}:
            break
        key, _, value = line.decode("ascii", errors="replace").partition(":")
        headers[key.lower()] = value.strip()
    length = int(headers.get("content-length") or 0)
    if length <= 0:
        raise MCPTransportError("MCP response missing Content-Length")
    return json.loads((await reader.readexactly(length)).decode("utf-8"))


async def _stdio_request(command: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *shlex.split(command),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if not process.stdin or not process.stdout:
        raise MCPTransportError("Failed to open MCP stdio pipes")
    try:
        request_id = 1
        initialize = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "chronos", "version": "0.1"},
            },
        }
        process.stdin.write(_encode_message(initialize))
        await process.stdin.drain()
        await _read_message(process.stdout)
        process.stdin.write(_encode_message({"jsonrpc": JSONRPC_VERSION, "method": "notifications/initialized", "params": {}}))
        await process.stdin.drain()

        request_id += 1
        process.stdin.write(_encode_message({"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method, "params": params or {}}))
        await process.stdin.drain()
        response = await asyncio.wait_for(_read_message(process.stdout), timeout=15)
        if response.get("error"):
            raise MCPTransportError(str(response["error"]))
        return response.get("result") or {}
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def _remote_request(server_url: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    # A registered MCP server_url must not be used to reach internal services
    # (cloud metadata, loopback admin APIs) via the backend.
    from core.ssrf import assert_safe_url, UnsafeURLError
    try:
        assert_safe_url(server_url)
    except UnsafeURLError as exc:
        raise MCPTransportError(f"remote MCP server_url blocked: {exc}") from exc
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            server_url,
            json={"jsonrpc": JSONRPC_VERSION, "id": 1, "method": method, "params": params or {}},
            headers={"accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    if payload.get("error"):
        raise MCPTransportError(str(payload["error"]))
    return payload.get("result") or {}


async def call_mcp(server: dict[str, Any], method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    transport = server.get("transport")
    if transport == "local":
        command = server.get("command")
        if not command:
            raise MCPTransportError("Local MCP server has no command")
        return await _stdio_request(command, method, params)
    if transport == "remote":
        server_url = server.get("server_url")
        if not server_url:
            raise MCPTransportError("Remote MCP server has no server_url")
        return await _remote_request(server_url, method, params)
    raise MCPTransportError(f"Unsupported MCP transport: {transport}")


class MCPConnector:
    async def execute(self, tool: str, args: dict[str, Any], agent: AgentContext) -> ToolResult:
        args.pop("__connector_tier", None)
        _, server_id, tool_name = tool.split(".", 2)
        repo = DatabaseConnectorRepository()
        server = await repo.get_mcp_server(server_id, tenant_id=agent.org_id)
        if not server:
            raise ValueError(f"MCP server not found: {server_id}")
        result = await call_mcp(server, "tools/call", {"name": tool_name, "arguments": args})
        return ToolResult(data={"server_id": server_id, "tool": tool_name, "result": result}, summary=f"MCP {tool_name} executed")


mcp_connector = MCPConnector()
