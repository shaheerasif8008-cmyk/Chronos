from __future__ import annotations

"""Unified platform capability — discover and operate ANY connected platform.

This is the general "use a platform to do a task" gateway. Rather than a bespoke
connector per service (Google, Canva, Notion, …), it exposes four broker-governed
tools that work across every integration mechanism Chronos already has:

  platform.list     → what platforms are connected and what kind they are
  platform.actions  → what a given platform can do (with input schemas)
  platform.invoke   → perform one action on a platform (the generic execution path)
  platform.connect  → register/authorize a new platform during a task

Backends, resolved by platform ``kind``:
  - "mcp"     → any connected MCP server (tools/list + tools/call)
  - "api"     → any OAuth2 REST app from the catalog (generic HTTP method+endpoint)
  - "browser" → operate any website with no API (handled by the browser tools)

The module exposes small seams (``_get_repo``, ``_discover_mcp``,
``_connected_providers``, ``_invoke_mcp``, ``_invoke_api``) so the logic is unit
testable without a database or live network.
"""

from typing import Any

from core.models import AgentContext, ToolResult


# ── seams (monkeypatched in tests) ────────────────────────────────────────────


def _get_repo():
    """Return the connector repository (MCP server registry lives here)."""
    from connectors.framework.repository import DatabaseConnectorRepository

    return DatabaseConnectorRepository()


async def _discover_mcp(server_id: str, org_id: str) -> dict[str, Any]:
    """Discover an MCP server's tools (tools/list), logging the discovery."""
    from connectors.framework.mcp import MCPDiscoveryService

    return await MCPDiscoveryService(_get_repo()).discover(server_id, tenant_id=org_id)


async def _connected_providers(org_id: str, member_id: str | None = None) -> list[dict[str, Any]]:
    """Return caller-owned and explicitly org-shared active REST/OAuth rows."""
    from sqlalchemy import select

    from core.db import engine, reflect_table
    from core.connector_tools import member_connector_clause

    connectors = await reflect_table("connectors")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    connectors.c.provider,
                    connectors.c.account_handle,
                    connectors.c.status,
                ).where(
                    connectors.c.organization_id == org_id,
                    connectors.c.status == "active",
                    member_connector_clause(connectors, org_id, member_id),
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def _invoke_mcp(
    agent: AgentContext, server: dict[str, Any], action: str, action_args: dict[str, Any]
) -> dict[str, Any]:
    """Call one MCP tool through the broker so the real action is governed.

    Routing via ``tool_broker.execute`` (rather than reaching into the MCP client
    directly) means the underlying ``mcp.<server>.<action>`` call gets its own
    permission check, audit record, and safety/rate gating — RULE 1.
    """
    from core import tool_broker

    server_id = str(server.get("id"))
    result = await tool_broker.execute(agent, f"mcp.{server_id}.{action}", dict(action_args))
    return result.data.get("result", result.data)


async def _invoke_api(
    agent: AgentContext, provider: str, action_args: dict[str, Any]
) -> ToolResult:
    """Make one authenticated REST call through the broker (governed gateway).

    The broker resolves the connector's ``vault_ref`` and routes to the generic
    HTTP connector, so the credential never touches this module and the call is
    permission-checked and audited like any other tool — RULE 1.
    """
    from core import tool_broker

    return await tool_broker.execute(agent, f"{provider}.api", dict(action_args))


# ── helpers ───────────────────────────────────────────────────────────────────


def _app_catalog() -> dict[str, Any]:
    from connectors.oauth_apps import APPS

    return APPS


def _is_mcp_id(platform_id: str) -> bool:
    return platform_id.startswith("mcp:")


def _mcp_server_id(platform_id: str) -> str:
    return platform_id[len("mcp:"):] if _is_mcp_id(platform_id) else platform_id


# ── connector ────────────────────────────────────────────────────────────────


class PlatformConnector:
    async def execute(self, tool: str, args: dict[str, Any], agent: AgentContext) -> ToolResult:
        approved_by_gate = bool(args.pop("__approved_by_gate", False))
        approval_id = str(args.pop("__approval_id", "") or "")
        idempotency_key = args.pop("__idempotency_key", None)
        governance = {}
        if approved_by_gate:
            governance["__approved_by_gate"] = True
        if approval_id:
            governance["__approval_id"] = approval_id
        if idempotency_key:
            governance["__idempotency_key"] = idempotency_key
        args.pop("__connector_tier", None)
        args.pop("__org_id", None)
        args.pop("__task_id", None)
        org_id = agent.org_id

        if tool == "platform.list":
            return await self._list(org_id, agent.member_id)
        if tool == "platform.actions":
            return await self._actions(args, org_id)
        if tool == "platform.invoke":
            return await self._invoke(args, agent, governance)
        if tool == "platform.connect":
            return await self._connect(args, org_id)
        raise ValueError(f"Unknown platform tool: {tool}")

    # ── platform.list ─────────────────────────────────────────────────────────

    async def _list(self, org_id: str, member_id: str | None = None) -> ToolResult:
        platforms: list[dict[str, Any]] = []

        # MCP servers (Google Drive, Canva, custom servers, …).
        try:
            servers = await _get_repo().list_mcp_servers(tenant_id=org_id)
        except Exception:
            servers = []
        for s in servers or []:
            platforms.append({
                "id": f"mcp:{s.get('id')}",
                "kind": "mcp",
                "name": s.get("name") or s.get("id"),
                "status": s.get("status") or "active",
                "summary": f"MCP server ({s.get('transport') or 'remote'})",
            })

        # Connected OAuth2/REST apps.
        catalog = _app_catalog()
        try:
            connected = (
                await _connected_providers(org_id, member_id)
                if member_id
                else await _connected_providers(org_id)
            )
        except Exception:
            connected = []
        for row in connected:
            provider = str(row.get("provider") or "")
            app = catalog.get(provider)
            platforms.append({
                "id": provider,
                "kind": "api",
                "name": app.name if app else provider,
                "status": row.get("status") or "active",
                "account": row.get("account_handle"),
                "summary": app.description if app else f"Connected {provider} app",
            })

        # Browser is always available (operate any website, no connection needed).
        platforms.append({
            "id": "browser",
            "kind": "browser",
            "name": "Web Browser",
            "status": "available",
            "summary": "Operate any website directly when it has no API or MCP server.",
        })

        return ToolResult(
            data={"platforms": platforms, "count": len(platforms)},
            summary=f"{len(platforms)} platform(s) available: "
                    + ", ".join(p["name"] for p in platforms[:8]),
        )

    # ── platform.actions ──────────────────────────────────────────────────────

    async def _actions(self, args: dict[str, Any], org_id: str) -> ToolResult:
        platform_id = str(args.get("platform_id") or "")
        if not platform_id:
            return ToolResult(
                data={"status": "error", "reason": "platform_id is required"},
                summary="platform.actions: platform_id is required",
            )

        if platform_id == "browser":
            return ToolResult(
                data={
                    "platform_id": "browser", "kind": "browser",
                    "actions": [{"name": "browser__navigate"}, {"name": "browser__click"},
                                {"name": "browser__type"}, {"name": "browser__extract"}],
                    "note": "Use the browser tools to operate any website.",
                },
                summary="Browser: use the browser__* tools to operate any website.",
            )

        if _is_mcp_id(platform_id):
            server_id = _mcp_server_id(platform_id)
            discovery = await _discover_mcp(server_id, org_id)
            if discovery.get("status") != "healthy":
                return ToolResult(
                    data={"status": "error", "platform_id": platform_id,
                          "reason": discovery.get("message") or "discovery failed", "actions": []},
                    summary=f"platform.actions: could not discover {platform_id}: "
                            f"{discovery.get('message')}",
                )
            actions = []
            for t in discovery.get("tools") or []:
                actions.append({
                    "name": t.get("name"),
                    "description": t.get("description") or "",
                    "parameters": t.get("inputSchema") or {"type": "object"},
                    "annotations": t.get("annotations") or {},
                })
            return ToolResult(
                data={"platform_id": platform_id, "kind": "mcp", "actions": actions,
                      "invoke_with": "platform.invoke(platform_id, action, action_args)"},
                summary=f"{len(actions)} action(s) on {platform_id}; call with platform.invoke.",
            )

        # REST/OAuth app.
        app = _app_catalog().get(platform_id)
        if not app:
            return ToolResult(
                data={"status": "error", "reason": f"unknown platform: {platform_id}", "actions": []},
                summary=f"platform.actions: unknown platform {platform_id!r}",
            )
        actions = [{"name": a, "description": f"{a} via {app.name} REST API"} for a in app.actions]
        return ToolResult(
            data={
                "platform_id": platform_id, "kind": "api", "actions": actions,
                "api_base": app.api_base,
                "invoke_with": "platform.invoke(platform_id, action_args={method, endpoint, params?, body?})",
                "note": "This is a generic REST app: choose the HTTP method and endpoint path.",
            },
            summary=f"{app.name}: {len(actions)} action group(s) via generic REST (platform.invoke).",
        )

    # ── platform.invoke ───────────────────────────────────────────────────────

    async def _invoke(
        self,
        args: dict[str, Any],
        agent: AgentContext,
        governance: dict[str, Any] | None = None,
    ) -> ToolResult:
        platform_id = str(args.get("platform_id") or "")
        action = str(args.get("action") or "")
        action_args = args.get("action_args")
        if not isinstance(action_args, dict):
            action_args = {}
        action_args = {**action_args, **(governance or {})}
        if not platform_id:
            return ToolResult(
                data={"status": "error", "reason": "platform_id is required"},
                summary="platform.invoke: platform_id is required",
            )

        if platform_id == "browser":
            return ToolResult(
                data={"status": "error", "reason": "use the browser__* tools directly for the browser platform"},
                summary="platform.invoke: operate the browser via the dedicated browser tools.",
            )

        if _is_mcp_id(platform_id):
            if not action:
                return ToolResult(
                    data={"status": "error", "reason": "action is required for an MCP platform"},
                    summary="platform.invoke: action (MCP tool name) is required",
                )
            server = await _get_repo().get_mcp_server(_mcp_server_id(platform_id), tenant_id=agent.org_id)
            if not server:
                return ToolResult(
                    data={"status": "error", "reason": f"MCP server not found: {platform_id}"},
                    summary=f"platform.invoke: MCP server {platform_id} not found",
                )
            try:
                result = await _invoke_mcp(agent, server, action, action_args)
            except Exception as exc:
                return ToolResult(
                    data={"status": "error", "reason": f"{type(exc).__name__}: {exc}"},
                    summary=f"platform.invoke: {platform_id}.{action} failed: {exc}",
                )
            return ToolResult(
                data={"status": "success", "platform_id": platform_id, "action": action, "result": result},
                summary=f"Invoked {action} on {platform_id}.",
            )

        # REST/OAuth app.
        if platform_id not in _app_catalog():
            return ToolResult(
                data={"status": "error", "reason": f"unknown platform: {platform_id}"},
                summary=f"platform.invoke: unknown platform {platform_id!r}",
            )
        try:
            api_result = await _invoke_api(agent, platform_id, action_args)
        except Exception as exc:
            return ToolResult(
                data={"status": "error", "reason": f"{type(exc).__name__}: {exc}"},
                summary=f"platform.invoke: {platform_id} REST call failed: {exc}",
            )
        return ToolResult(
            data={"status": "success", "platform_id": platform_id, "result": api_result.data},
            summary=f"Invoked {platform_id}: {api_result.summary}",
        )

    # ── platform.connect ──────────────────────────────────────────────────────

    async def _connect(self, args: dict[str, Any], org_id: str) -> ToolResult:
        """Connect a new platform during a task.

        - kind="mcp": register a remote MCP server by URL — fully autonomous.
        - kind="api": return the OAuth consent URL to authorize the app. Provider
          consent screens require a human to authenticate (a provider constraint,
          not a Chronos gate), so the agent should drive it via browser login.
        """
        kind = str(args.get("kind") or "").lower()

        if kind == "mcp":
            name = str(args.get("name") or "").strip()
            server_url = str(args.get("server_url") or "").strip()
            if not name or not server_url:
                return ToolResult(
                    data={"status": "error", "reason": "name and server_url are required for an MCP connection"},
                    summary="platform.connect: name and server_url are required",
                )
            try:
                server = await _get_repo().register_mcp_server(
                    tenant_id=org_id, name=name, transport="remote", server_url=server_url,
                )
            except Exception as exc:
                return ToolResult(
                    data={"status": "error", "reason": f"{type(exc).__name__}: {exc}"},
                    summary=f"platform.connect: failed to register MCP server: {exc}",
                )
            server_id = server.get("id")
            discovery = await _discover_mcp(str(server_id), org_id)
            return ToolResult(
                data={"status": "connected", "platform_id": f"mcp:{server_id}", "kind": "mcp",
                      "tools_discovered": discovery.get("tools_discovered", 0)},
                summary=f"Connected MCP server '{name}' ({discovery.get('tools_discovered', 0)} tools).",
            )

        if kind in {"api", "oauth"}:
            provider = str(args.get("provider") or "")
            app = _app_catalog().get(provider)
            if not app:
                return ToolResult(
                    data={"status": "error", "reason": f"unknown app: {provider}"},
                    summary=f"platform.connect: unknown app {provider!r}",
                )
            return ToolResult(
                data={"status": "authorization_required", "provider": provider, "kind": "api",
                      "auth_url": app.auth_url, "scopes": app.scopes,
                      "next": "Open the auth_url to authorize. Use browser__login_task to "
                              "drive the consent flow (the user signs in at the provider)."},
                summary=f"{app.name} needs authorization — open the consent URL to connect.",
            )

        return ToolResult(
            data={"status": "error", "reason": "kind must be 'mcp' or 'api'"},
            summary="platform.connect: kind must be 'mcp' or 'api'",
        )


platform_connector = PlatformConnector()
