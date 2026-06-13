"""Acceptance proof for the unified platform capability.

Covers platform.list / actions / invoke / connect across the MCP, REST-app, and
browser backends. The connector's I/O seams (_get_repo, _discover_mcp,
_connected_providers, _invoke_mcp, _invoke_api, _app_catalog) are monkeypatched
so these tests need no database or network.
"""
from __future__ import annotations

import types
import uuid

import pytest

import connectors.platform as pf
from core.models import AgentContext


def _agent(org_id: str = "org1") -> AgentContext:
    return AgentContext(id=str(uuid.uuid4()), org_id=org_id,
                        task_id=str(uuid.uuid4()), member_id=str(uuid.uuid4()))


class _FakeRepo:
    def __init__(self, servers=None, server=None, registered=None):
        self._servers = servers or []
        self._server = server
        self._registered = registered or {"id": "new-server"}

    async def list_mcp_servers(self, *, tenant_id):
        return self._servers

    async def get_mcp_server(self, server_id, *, tenant_id):
        return self._server

    async def register_mcp_server(self, *, tenant_id, name, transport, command=None, server_url=None):
        return self._registered


def _fake_app():
    return types.SimpleNamespace(
        name="Notion", description="Docs & databases", api_base="https://api.notion.com",
        actions=["search", "read", "write"], auth_url="https://notion.so/oauth",
        scopes=["read", "write"],
    )


# ── platform.list ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_includes_mcp_api_and_browser(monkeypatch):
    monkeypatch.setattr(pf, "_get_repo",
                        lambda: _FakeRepo(servers=[{"id": "s1", "name": "Google Drive", "transport": "remote", "status": "active"}]))

    async def fake_connected(org_id):
        return [{"provider": "notion", "account_handle": "acme", "status": "active"}]

    monkeypatch.setattr(pf, "_connected_providers", fake_connected)
    monkeypatch.setattr(pf, "_app_catalog", lambda: {"notion": _fake_app()})

    result = await pf.platform_connector.execute("platform.list", {}, _agent())
    kinds = {p["kind"] for p in result.data["platforms"]}
    assert kinds == {"mcp", "api", "browser"}
    ids = {p["id"] for p in result.data["platforms"]}
    assert "mcp:s1" in ids and "notion" in ids and "browser" in ids


@pytest.mark.asyncio
async def test_list_degrades_when_sources_fail(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(pf, "_get_repo", boom)

    async def fake_connected(org_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(pf, "_connected_providers", fake_connected)
    result = await pf.platform_connector.execute("platform.list", {}, _agent())
    # Browser is always present even when other sources error.
    assert any(p["id"] == "browser" for p in result.data["platforms"])


# ── platform.actions ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_actions_mcp_returns_tool_schemas(monkeypatch):
    async def fake_discover(server_id, org_id):
        assert server_id == "s1"
        return {"status": "healthy", "tools": [
            {"name": "create_file", "description": "Create a file",
             "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}}},
        ]}

    monkeypatch.setattr(pf, "_discover_mcp", fake_discover)
    result = await pf.platform_connector.execute(
        "platform.actions", {"platform_id": "mcp:s1"}, _agent())
    assert result.data["kind"] == "mcp"
    action = result.data["actions"][0]
    assert action["name"] == "create_file"
    assert action["parameters"]["properties"]["title"]["type"] == "string"


@pytest.mark.asyncio
async def test_actions_mcp_discovery_failure(monkeypatch):
    async def fake_discover(server_id, org_id):
        return {"status": "error", "message": "unreachable"}

    monkeypatch.setattr(pf, "_discover_mcp", fake_discover)
    result = await pf.platform_connector.execute(
        "platform.actions", {"platform_id": "mcp:dead"}, _agent())
    assert result.data["status"] == "error"
    assert "unreachable" in result.data["reason"]


@pytest.mark.asyncio
async def test_actions_api_app(monkeypatch):
    monkeypatch.setattr(pf, "_app_catalog", lambda: {"notion": _fake_app()})
    result = await pf.platform_connector.execute(
        "platform.actions", {"platform_id": "notion"}, _agent())
    assert result.data["kind"] == "api"
    assert {a["name"] for a in result.data["actions"]} == {"search", "read", "write"}
    assert result.data["api_base"] == "https://api.notion.com"


@pytest.mark.asyncio
async def test_actions_unknown_platform(monkeypatch):
    monkeypatch.setattr(pf, "_app_catalog", lambda: {})
    result = await pf.platform_connector.execute(
        "platform.actions", {"platform_id": "nope"}, _agent())
    assert result.data["status"] == "error"


@pytest.mark.asyncio
async def test_actions_requires_platform_id():
    result = await pf.platform_connector.execute("platform.actions", {}, _agent())
    assert result.data["status"] == "error"


# ── platform.invoke ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_mcp_success(monkeypatch):
    monkeypatch.setattr(pf, "_get_repo", lambda: _FakeRepo(server={"id": "s1", "transport": "remote"}))

    async def fake_invoke(server, action, action_args):
        return {"content": [{"type": "text", "text": "ok"}], "echo": action_args}

    monkeypatch.setattr(pf, "_invoke_mcp", fake_invoke)
    result = await pf.platform_connector.execute(
        "platform.invoke",
        {"platform_id": "mcp:s1", "action": "create_file", "action_args": {"title": "Lab"}},
        _agent())
    assert result.data["status"] == "success"
    assert result.data["result"]["echo"] == {"title": "Lab"}


@pytest.mark.asyncio
async def test_invoke_mcp_server_missing(monkeypatch):
    monkeypatch.setattr(pf, "_get_repo", lambda: _FakeRepo(server=None))
    result = await pf.platform_connector.execute(
        "platform.invoke", {"platform_id": "mcp:gone", "action": "x"}, _agent())
    assert result.data["status"] == "error"


@pytest.mark.asyncio
async def test_invoke_api_success(monkeypatch):
    from core.models import ToolResult
    monkeypatch.setattr(pf, "_app_catalog", lambda: {"notion": _fake_app()})

    async def fake_invoke_api(agent, provider, action_args):
        assert provider == "notion"
        return ToolResult(data={"results": [1, 2]}, summary="notion GET /search → 2 results")

    monkeypatch.setattr(pf, "_invoke_api", fake_invoke_api)
    result = await pf.platform_connector.execute(
        "platform.invoke",
        {"platform_id": "notion", "action_args": {"method": "GET", "endpoint": "/search"}},
        _agent())
    assert result.data["status"] == "success"
    assert result.data["result"] == {"results": [1, 2]}


@pytest.mark.asyncio
async def test_invoke_unknown_platform(monkeypatch):
    monkeypatch.setattr(pf, "_app_catalog", lambda: {})
    result = await pf.platform_connector.execute(
        "platform.invoke", {"platform_id": "nope", "action_args": {}}, _agent())
    assert result.data["status"] == "error"


@pytest.mark.asyncio
async def test_invoke_browser_redirects_to_tools():
    result = await pf.platform_connector.execute(
        "platform.invoke", {"platform_id": "browser"}, _agent())
    assert result.data["status"] == "error"
    assert "browser" in result.data["reason"]


# ── platform.connect ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_mcp_autonomous(monkeypatch):
    monkeypatch.setattr(pf, "_get_repo", lambda: _FakeRepo(registered={"id": "srv-9"}))

    async def fake_discover(server_id, org_id):
        assert server_id == "srv-9"
        return {"status": "healthy", "tools_discovered": 5}

    monkeypatch.setattr(pf, "_discover_mcp", fake_discover)
    result = await pf.platform_connector.execute(
        "platform.connect",
        {"kind": "mcp", "name": "My Server", "server_url": "https://mcp.example.com"},
        _agent())
    assert result.data["status"] == "connected"
    assert result.data["platform_id"] == "mcp:srv-9"
    assert result.data["tools_discovered"] == 5


@pytest.mark.asyncio
async def test_connect_mcp_requires_url():
    result = await pf.platform_connector.execute(
        "platform.connect", {"kind": "mcp", "name": "x"}, _agent())
    assert result.data["status"] == "error"


@pytest.mark.asyncio
async def test_connect_api_returns_auth_url(monkeypatch):
    monkeypatch.setattr(pf, "_app_catalog", lambda: {"notion": _fake_app()})
    result = await pf.platform_connector.execute(
        "platform.connect", {"kind": "api", "provider": "notion"}, _agent())
    assert result.data["status"] == "authorization_required"
    assert result.data["auth_url"] == "https://notion.so/oauth"


@pytest.mark.asyncio
async def test_connect_bad_kind():
    result = await pf.platform_connector.execute(
        "platform.connect", {"kind": "carrier-pigeon"}, _agent())
    assert result.data["status"] == "error"


@pytest.mark.asyncio
async def test_unknown_tool_raises():
    with pytest.raises(ValueError):
        await pf.platform_connector.execute("platform.bogus", {}, _agent())
