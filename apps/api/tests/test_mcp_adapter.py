"""Item 4 — the MCP connector adapter executes through the real JSON-RPC client."""
import pytest

from connectors.framework.adapters import MCPAdapter
from connectors.framework.models import ConnectorDef


def _mcp_connector(mcp_config):
    return ConnectorDef(
        id="mcp_test",
        name="Test MCP",
        provider="mcp",
        description="test",
        type="mcp",
        auth_type="remote_mcp",
        scopes=["mcp.execute"],
        actions=[],
        mcp_config=mcp_config,
    )


@pytest.mark.asyncio
async def test_list_actions_normalizes_discovered_tools(monkeypatch):
    import connectors.mcp_client as mcp_client

    async def fake_call(server, method, params=None):
        assert server == {"transport": "remote", "server_url": "https://mcp.test"}
        assert method == "tools/list"
        return {"tools": [{"name": "lookup", "description": "Lookup a record", "inputSchema": {"type": "object"}}]}

    monkeypatch.setattr(mcp_client, "call_mcp", fake_call)

    adapter = MCPAdapter(_mcp_connector({"transport": "remote", "server_url": "https://mcp.test"}))
    actions = await adapter.list_actions()

    assert [a.name for a in actions] == ["lookup"]
    assert actions[0].required_permissions == ["mcp.execute"]
    assert actions[0].approval_required is True


@pytest.mark.asyncio
async def test_execute_delegates_to_call_mcp(monkeypatch):
    import connectors.mcp_client as mcp_client

    captured = {}

    async def fake_call(server, method, params=None):
        captured["method"] = method
        captured["params"] = params
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(mcp_client, "call_mcp", fake_call)

    adapter = MCPAdapter(_mcp_connector({"transport": "local", "command": "run-mcp"}))
    result = await adapter.execute("lookup", {"id": 7}, {})

    assert result.status == "success"
    assert captured["method"] == "tools/call"
    assert captured["params"] == {"name": "lookup", "arguments": {"id": 7}}
    assert result.output == {"result": {"content": [{"type": "text", "text": "ok"}]}}


@pytest.mark.asyncio
async def test_execute_without_config_fails_truthfully():
    adapter = MCPAdapter(_mcp_connector(None))
    result = await adapter.execute("lookup", {}, {})
    assert result.status == "failure"
    assert "Register it via /connectors/mcp/servers" in result.error
