"""
Composio connector — broker-facing adapter for all Composio-managed SaaS apps.

The ToolBroker routes every Composio-managed provider here (RULE 1: no direct
connector calls). This module translates a Chronos `provider.action` tool call
into a Composio action execution against the caller's managed-auth entity, then
normalises the response into a ToolResult.

When Composio isn't configured, or the call runs under a demo/fixture tier, it
returns clearly-labelled placeholder data instead of hitting the network.
"""
from __future__ import annotations

import logging
from typing import Any

from connectors import composio_client
from core.models import AgentContext, ToolResult

log = logging.getLogger(__name__)


def _normalise_response(tool: str, response: dict[str, Any]) -> ToolResult:
    """Turn a raw Composio SDK response into a ToolResult."""
    successful = response.get("successful")
    if successful is None:
        successful = response.get("success", True)
    data = response.get("data")
    if not isinstance(data, dict):
        data = {"result": data} if data is not None else {}

    if not successful:
        error = response.get("error") or "Composio action failed"
        return ToolResult(data={"error": str(error)}, summary=f"{tool} failed: {error}")

    return ToolResult(data=data, summary=f"{tool} → ok")


class ComposioConnector:
    """Routes Composio-managed `provider.action` calls through Composio managed auth."""

    async def execute(self, tool: str, args: dict[str, Any], agent: AgentContext) -> ToolResult:
        provider = tool.split(".")[0]
        tier = args.pop("__connector_tier", "live")
        org_id = str(args.pop("__org_id", "") or agent.org_id)
        args.pop("__task_id", None)

        if tier in {"demo", "fixture"} or not composio_client.is_configured():
            reason = (
                "demo tier" if tier in {"demo", "fixture"} else "COMPOSIO_API_KEY is not set"
            )
            return ToolResult(
                data={"demo": True, "tool": tool, "reason": reason},
                summary=f"[demo] {tool} — connect {provider} via Composio to use live data",
            )

        try:
            action, params = composio_client.resolve_action(tool, args)
        except ValueError as exc:
            return ToolResult(data={"error": str(exc)}, summary=f"{tool} failed: {exc}")

        entity = composio_client.entity_id(org_id, agent.member_id)
        try:
            response = await composio_client.execute_action(action, params, entity=entity)
        except Exception as exc:  # noqa: BLE001 - SDK raises a wide range of errors
            log.warning("Composio %s (action=%s) failed: %s", tool, action, exc)
            return ToolResult(data={"error": str(exc)}, summary=f"{tool} failed: {exc}")

        if not isinstance(response, dict):
            response = {"successful": True, "data": response}
        return _normalise_response(tool, response)


composio_connector = ComposioConnector()
