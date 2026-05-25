from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from connectors.framework.models import ConnectorActionDef, ConnectorDef, ConnectorResult


class ConnectorAdapter(Protocol):
    connector: ConnectorDef

    async def list_actions(self) -> list[ConnectorActionDef]:
        ...

    async def execute(self, action_name: str, args: dict[str, Any], context: dict[str, Any]) -> ConnectorResult:
        ...

    async def validate_credentials(self, credentials: dict[str, Any]) -> bool:
        ...


class InternalEchoAdapter:
    connector = ConnectorDef(
        id="internal_echo",
        name="Internal Echo",
        provider="internal",
        description="Echoes validated input through the real connector runtime.",
        type="internal",
        auth_type="none",
        scopes=["internal.echo"],
        actions=[
            ConnectorActionDef(
                name="echo",
                description="Echo a message after schema validation and permission checks.",
                parameters_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string", "description": "Message to echo."}},
                    "required": ["message"],
                },
                output_schema={"type": "object", "properties": {"message": {"type": "string"}}},
                required_permissions=["internal.echo"],
                risk_level="read",
                approval_required=False,
            )
        ],
    )

    async def list_actions(self) -> list[ConnectorActionDef]:
        return self.connector.actions

    async def execute(self, action_name: str, args: dict[str, Any], context: dict[str, Any]) -> ConnectorResult:
        if action_name != "echo":
            return ConnectorResult(status="failure", error=f"Unknown action: {action_name}")
        return ConnectorResult(status="success", output={"message": args["message"]})

    async def validate_credentials(self, credentials: dict[str, Any]) -> bool:
        return True


class InternalTimeAdapter:
    connector = ConnectorDef(
        id="internal_time",
        name="Internal Time",
        provider="internal",
        description="Returns current server time and timezone through the connector runtime.",
        type="internal",
        auth_type="none",
        scopes=["internal.time"],
        actions=[
            ConnectorActionDef(
                name="now",
                description="Return the current server time.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "timezone": {
                            "type": "string",
                            "description": "IANA timezone, default UTC.",
                        }
                    },
                    "required": [],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "iso": {"type": "string"},
                        "timezone": {"type": "string"},
                    },
                },
                required_permissions=["internal.time"],
                risk_level="read",
                approval_required=False,
            )
        ],
    )

    async def list_actions(self) -> list[ConnectorActionDef]:
        return self.connector.actions

    async def execute(self, action_name: str, args: dict[str, Any], context: dict[str, Any]) -> ConnectorResult:
        if action_name != "now":
            return ConnectorResult(status="failure", error=f"Unknown action: {action_name}")
        tz_name = args.get("timezone") or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            return ConnectorResult(status="validation_error", error=f"Unknown timezone: {tz_name}")
        now = datetime.now(tz)
        return ConnectorResult(status="success", output={"iso": now.isoformat(), "timezone": tz_name})

    async def validate_credentials(self, credentials: dict[str, Any]) -> bool:
        return True


class BrowserAdapter:
    connector = ConnectorDef(
        id="browser",
        name="Browser",
        provider="browser",
        description="Search, fetch, and extract structured page data through an isolated Playwright browser context.",
        type="native",
        auth_type="none",
        scopes=["browser.search", "browser.fetch", "browser.extract_contacts"],
        actions=[
            ConnectorActionDef(
                name="search",
                description="Search the web and return structured result snippets.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query."},
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum result count.",
                            "minimum": 1,
                            "maximum": 20,
                            "default": 10,
                        },
                    },
                    "required": ["query"],
                },
                output_schema={"type": "object"},
                required_permissions=["browser.search"],
                risk_level="read",
                approval_required=False,
            ),
            ConnectorActionDef(
                name="fetch",
                description="Fetch a URL and return readable page text.",
                parameters_schema={
                    "type": "object",
                    "properties": {"url": {"type": "string", "description": "URL to fetch."}},
                    "required": ["url"],
                },
                output_schema={"type": "object"},
                required_permissions=["browser.fetch"],
                risk_level="read",
                approval_required=False,
            ),
            ConnectorActionDef(
                name="extract_contacts",
                description="Extract emails, phone numbers, and likely people/title lines from a page.",
                parameters_schema={
                    "type": "object",
                    "properties": {"url": {"type": "string", "description": "URL to inspect."}},
                    "required": ["url"],
                },
                output_schema={"type": "object"},
                required_permissions=["browser.extract_contacts"],
                risk_level="read",
                approval_required=False,
            ),
        ],
    )

    async def list_actions(self) -> list[ConnectorActionDef]:
        return self.connector.actions

    async def execute(self, action_name: str, args: dict[str, Any], context: dict[str, Any]) -> ConnectorResult:
        from connectors.browser import browser_connector

        tool = f"browser.{action_name}"
        try:
            result = await browser_connector.execute(tool, dict(args))
        except ValueError as exc:
            return ConnectorResult(status="validation_error", error=str(exc))
        return ConnectorResult(status="success", output={"summary": result.summary, "data": result.data})

    async def validate_credentials(self, credentials: dict[str, Any]) -> bool:
        return True


class MCPAdapter:
    """Architecture placeholder for MCP connectors.

    This normalizes discovered MCP tools into Chronos connector actions. Actual
    MCP transport is intentionally not marked production-ready until a concrete
    MCP server command/URL can be connected and tested.
    """

    def __init__(self, connector: ConnectorDef):
        self.connector = connector

    async def list_actions(self) -> list[ConnectorActionDef]:
        # TODO: discover tools from mcp_server_url or command config.
        return []

    async def execute(self, action_name: str, args: dict[str, Any], context: dict[str, Any]) -> ConnectorResult:
        return ConnectorResult(status="failure", error="MCP transport is not implemented for this connector")

    async def validate_credentials(self, credentials: dict[str, Any]) -> bool:
        return True


def adapter_registry() -> dict[str, ConnectorAdapter]:
    adapters: list[ConnectorAdapter] = [InternalEchoAdapter(), InternalTimeAdapter(), BrowserAdapter()]
    return {adapter.connector.id: adapter for adapter in adapters}
