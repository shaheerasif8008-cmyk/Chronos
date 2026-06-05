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


def _generic_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "method": {"type": "string", "description": "HTTP method."},
            "endpoint": {"type": "string", "description": "Path relative to the connector API base."},
            "params": {"type": "object", "description": "Query parameters."},
            "body": {"type": "object", "description": "JSON request body."},
        },
        "required": ["method", "endpoint"],
    }


def _search_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "endpoint": {"type": "string", "description": "Provider search/list endpoint."},
            "query": {"type": "string", "description": "Search query."},
            "limit": {"type": "integer", "description": "Maximum records to return."},
        },
        "required": ["endpoint", "query"],
    }


def _read_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Provider object identifier."},
            "endpoint": {"type": "string", "description": "Optional provider endpoint override."},
        },
        "required": ["id"],
    }


class OAuthHTTPAdapter:
    """Generic typed adapter for OAuth/API-key apps in the Phase 8 catalog.

    The connector framework owns install, permission, policy, queue, health, and
    audit. Execution delegates to the existing provider-specific HTTP helper
    shape and fails honestly when live credentials are absent.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        write_risk = "financial" if "financial" in app.risk_levels else (
            "external_message" if "external_message" in app.risk_levels else "write"
        )
        self.connector = ConnectorDef(
            id=app.id,
            name=app.name,
            provider=app.id,
            description=app.description,
            type="api_key" if app.auth_type in {"api_key", "signing_secret"} else "oauth",
            auth_type=app.auth_type,
            scopes=app.scopes,
            actions=[
                ConnectorActionDef(
                    name="search",
                    description=f"Search {app.name} records or messages using provider-scoped credentials.",
                    parameters_schema=_search_schema(),
                    output_schema={"type": "object"},
                    required_permissions=[app.scopes[0]] if app.scopes else [f"{app.id}.read"],
                    risk_level="read",
                    approval_required=False,
                ),
                ConnectorActionDef(
                    name="read",
                    description=f"Read one {app.name} object by id or endpoint.",
                    parameters_schema=_read_schema(),
                    output_schema={"type": "object"},
                    required_permissions=[app.scopes[0]] if app.scopes else [f"{app.id}.read"],
                    risk_level="read",
                    approval_required=False,
                ),
                ConnectorActionDef(
                    name="write",
                    description=f"Create or update {app.name} data through the governed connector policy.",
                    parameters_schema=_generic_request_schema(),
                    output_schema={"type": "object"},
                    required_permissions=app.scopes or [f"{app.id}.write"],
                    risk_level=write_risk,
                    approval_required=True,
                ),
            ],
        )

    async def list_actions(self) -> list[ConnectorActionDef]:
        return self.connector.actions

    async def execute(self, action_name: str, args: dict[str, Any], context: dict[str, Any]) -> ConnectorResult:
        credentials = context.get("credentials") or {}
        if not credentials:
            return ConnectorResult(status="failure", error="Connector credentials are missing")
        if action_name == "search":
            return await self._request(
                credentials,
                "GET",
                str(args["endpoint"]),
                params={"q": args["query"], "limit": args.get("limit")},
            )
        if action_name == "read":
            endpoint = str(args.get("endpoint") or args["id"])
            return await self._request(credentials, "GET", endpoint)
        if action_name == "write":
            return await self._request(
                credentials,
                str(args["method"]),
                str(args["endpoint"]),
                params=args.get("params"),
                body=args.get("body"),
            )
        return ConnectorResult(status="failure", error=f"Unknown action: {action_name}")

    async def validate_credentials(self, credentials: dict[str, Any]) -> bool:
        return bool(credentials) or self.app.auth_type == "none"

    async def _request(
        self,
        credentials: dict[str, Any],
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> ConnectorResult:
        import httpx

        api_base = str(credentials.get("api_base") or self.app.api_base or "").rstrip("/")
        if not api_base:
            return ConnectorResult(status="failure", error="Connector API base is not configured")

        url = api_base + "/" + endpoint.lstrip("/")
        headers: dict[str, str] = {}
        token = credentials.get("access_token") or credentials.get("api_key")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif self.app.auth_type != "none":
            return ConnectorResult(status="failure", error="Connector access token is missing")

        if self.app.id == "notion":
            headers["Notion-Version"] = "2022-06-28"
        if self.app.id == "github":
            headers["Accept"] = "application/vnd.github+json"

        clean_params = {key: value for key, value in (params or {}).items() if value is not None}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(
                    method.upper(),
                    url,
                    headers=headers,
                    params=clean_params or None,
                    json=body,
                )
        except httpx.HTTPError as exc:
            return ConnectorResult(status="failure", error=f"{self.app.id} request failed: {exc}")

        if response.status_code >= 400:
            return ConnectorResult(
                status="failure",
                error=f"{self.app.id} {method.upper()} {endpoint} returned {response.status_code}: {response.text[:240]}",
            )
        data = response.json() if response.content else {}
        return ConnectorResult(status="success", output={"data": data, "status_code": response.status_code})


class CustomHTTPAdapter(OAuthHTTPAdapter):
    def __init__(self, app: Any) -> None:
        super().__init__(app)
        self.connector = ConnectorDef(
            id=app.id,
            name=app.name,
            provider=app.id,
            description=app.description,
            type="api_key",
            auth_type=app.auth_type,
            scopes=app.scopes,
            actions=[
                ConnectorActionDef(
                    name="discover_schema",
                    description="Import or inspect an OpenAPI schema for this custom HTTP connector.",
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "openapi_url": {"type": "string", "description": "OpenAPI URL to inspect."},
                            "schema": {"type": "object", "description": "Inline OpenAPI schema."},
                        },
                        "required": [],
                    },
                    output_schema={"type": "object"},
                    required_permissions=["http.schema"],
                    risk_level="read",
                    approval_required=False,
                ),
                ConnectorActionDef(
                    name="request",
                    description="Execute a custom HTTP request through broker policy, redaction, health, and audit.",
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "method": {"type": "string", "description": "HTTP method."},
                            "url": {"type": "string", "description": "Absolute URL or path."},
                            "headers": {"type": "object", "description": "Headers; secrets are redacted in logs."},
                            "params": {"type": "object", "description": "Query parameters."},
                            "body": {"type": "object", "description": "JSON request body."},
                        },
                        "required": ["method", "url"],
                    },
                    output_schema={"type": "object"},
                    required_permissions=["http.request"],
                    risk_level="write",
                    approval_required=True,
                ),
            ],
        )


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
    from connectors.oauth_apps import APPS

    adapters: list[ConnectorAdapter] = [InternalEchoAdapter(), InternalTimeAdapter(), BrowserAdapter()]
    for app in APPS.values():
        if app.id == "remote_mcp":
            continue
        if app.id == "custom_http":
            adapters.append(CustomHTTPAdapter(app))
        else:
            adapters.append(OAuthHTTPAdapter(app))
    return {adapter.connector.id: adapter for adapter in adapters}
