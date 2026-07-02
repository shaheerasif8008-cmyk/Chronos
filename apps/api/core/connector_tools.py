"""
Connector-aware tool resolution — the Anthropic/Claude connectors model.

Claude.ai exposes a connector's tools to the model only when the connector is
actually connected, tells the model which connectors exist (so it can direct
the user to connect the rest), and lets users set per-tool permissions
(always allow / ask first / blocked). This module implements the same model
for Chronos:

- ``connected_providers``   — which SaaS connectors this org has live.
- ``resolve_agent_tools``   — filter a static tool list down to what is real
                              for this org (connected, not blocked, not
                              disabled for this conversation).
- ``connectors_prompt_block`` — the "# Connectors" system-prompt section so
                              the model always knows what is connected, what
                              could be connected, and where the user manages
                              them.
- ``provider_tool_specs``   — per-provider tool metadata for the connectors
                              settings UI (tool permissions panel).

Execution still always goes through tool_broker.execute() — this module only
shapes what the model can see.
"""
from __future__ import annotations

import logging
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)

# SaaS providers whose tools require a per-org/user connection before the
# model should see them. Native/local tool families (browser, fs, code, doc,
# computer, desktop, image, voice, data, chat_history, repo, platform, mcp)
# are always available and never filtered here.
SAAS_PROVIDER_LABELS: dict[str, str] = {
    "gmail": "Gmail",
    "google_calendar": "Google Calendar",
    "google_drive": "Google Drive",
    "notion": "Notion",
    "slack": "Slack",
    "github": "GitHub",
    "linear": "Linear",
    "hubspot": "HubSpot",
    "airtable": "Airtable",
    "jira": "Jira",
    "outlook": "Outlook",
    "teams": "Microsoft Teams",
    "sharepoint_onedrive": "SharePoint / OneDrive",
    "salesforce": "Salesforce",
    "stripe": "Stripe",
    "canva": "Canva",
}

_WRITE_MARKERS = (
    "draft", "send", "post", "publish", "write", "create", "update",
    "delete", "upload", "move", "copy", "autofill", "export",
)


def _tool_name(schema: dict[str, Any]) -> str:
    return str(((schema or {}).get("function") or {}).get("name") or "")


def tool_family(name: str) -> str:
    return name.partition("__")[0]


def tool_access(name: str) -> str:
    """Classify a registry tool as read or write for the permissions UI."""
    action = name.partition("__")[2]
    return "write" if any(marker in action for marker in _WRITE_MARKERS) else "read"


async def connected_providers(org_id: str) -> dict[str, str]:
    """Return {provider: account_handle} for every active connector row in *org_id*."""
    from sqlalchemy import select

    from core.db import engine, reflect_table

    connectors_table = await reflect_table("connectors")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    connectors_table.c.provider,
                    connectors_table.c.account_handle,
                ).where(
                    (connectors_table.c.organization_id == str(org_id))
                    & (connectors_table.c.status == "active")
                )
            )
        ).mappings().all()
    connected: dict[str, str] = {}
    for row in rows:
        provider = str(row["provider"])
        if provider not in connected or row.get("account_handle"):
            connected[provider] = str(row.get("account_handle") or "")
    return connected


async def registered_mcp_servers(org_id: str) -> list[dict[str, Any]]:
    """Return registered MCP servers (custom connectors) for the org."""
    try:
        from connectors.framework.repository import DatabaseConnectorRepository

        return await DatabaseConnectorRepository().list_mcp_servers(tenant_id=org_id)
    except Exception:  # noqa: BLE001 — custom connectors are additive, never fatal
        return []


async def resolve_agent_tools(
    base_tools: list[dict[str, Any]],
    *,
    org_id: str,
    disabled_tools: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter a static tool list down to what is real for this org.

    - SaaS connector tools are exposed only when the provider is connected
      (active connector row). Demo/fixture tiers keep their tools so local
      development without credentials still works end to end.
    - Tools blocked via per-tool permissions (Settings → Connectors) are
      removed entirely — the model never sees them.
    - ``disabled_tools`` removes families or exact names for one conversation
      (the in-chat "Search and tools" toggles).

    Degrades to the unfiltered list on any infrastructure error so a broken
    lookup can never take chat down.
    """
    try:
        from core.connector_health import connector_tier
        from core.settings_store import tool_permissions as org_tool_permissions

        connected = await connected_providers(org_id)
        permissions = await org_tool_permissions(org_id)
        disabled = {d.strip() for d in (disabled_tools or []) if d and d.strip()}

        tier_cache: dict[str, str] = {}

        async def _tier(provider: str) -> str:
            if provider not in tier_cache:
                tier_cache[provider] = await connector_tier(provider)
            return tier_cache[provider]

        resolved: list[dict[str, Any]] = []
        for schema in base_tools:
            name = _tool_name(schema)
            family = tool_family(name)
            broker_name = name.replace("__", ".", 1) if "__" in name else name
            if name in disabled or family in disabled:
                continue
            if permissions.get(broker_name) == "blocked" or permissions.get(name) == "blocked":
                continue
            if family in SAAS_PROVIDER_LABELS and family not in connected:
                # Unconnected SaaS provider: only demo/fixture tiers keep their
                # tools (placeholder data, clearly flagged by the broker).
                if await _tier(family) not in {"demo", "fixture"}:
                    continue
            resolved.append(schema)
        return resolved
    except Exception as exc:  # noqa: BLE001 — never let filtering break the loop
        logger.warning("Tool resolution degraded to static list: %s", exc)
        return list(base_tools)


async def connectors_prompt_block(org_id: str) -> str:
    """Build the "# Connectors" system-prompt section.

    Always present so the model knows the connectors system exists: which apps
    are connected (usable through tools right now), which are available but
    not connected (direct the user to Settings → Connectors), and which custom
    MCP servers are registered (usable via the platform__ tools).
    """
    try:
        connected = await connected_providers(org_id)
        servers = await registered_mcp_servers(org_id)
    except Exception:  # noqa: BLE001 — the block is advisory
        return ""

    lines = ["# Connectors"]
    live = [
        f"- {SAAS_PROVIDER_LABELS.get(p, p)}"
        # Composio entity ids ("org:x" / "org:member") are internal, not account names.
        + (f" ({handle})" if handle and ":" not in handle and not handle.startswith(org_id) else "")
        for p, handle in sorted(connected.items())
        if p in SAAS_PROVIDER_LABELS
    ]
    if live:
        lines.append("Connected apps (their tools are available to you now):")
        lines.extend(live)
    else:
        lines.append("No external apps are connected yet.")

    not_connected = sorted(
        label for p, label in SAAS_PROVIDER_LABELS.items() if p not in connected
    )
    if not_connected:
        lines.append(
            "Available to connect (NOT connected — you cannot use these yet): "
            + ", ".join(not_connected)
            + ". If the user asks for one of these, tell them to connect it in "
            "Settings → Connectors, then continue once connected."
        )

    active_servers = [s for s in servers if str(s.get("status") or "") != "disabled"]
    if active_servers:
        lines.append("Custom connectors (registered MCP servers):")
        for server in active_servers[:10]:
            lines.append(
                f"- {server.get('name')} (id: {server.get('id')}) — discover its tools "
                f"with platform__actions and call them with platform__invoke."
            )
    return "\n".join(lines)


def provider_tool_specs(provider: str) -> list[dict[str, Any]]:
    """Return the model-facing tools for one SaaS provider (for the settings UI)."""
    from runtime.tool_registry import ALL_TOOLS, ALWAYS_APPROVAL_TOOL_NAMES

    specs: list[dict[str, Any]] = []
    for schema in ALL_TOOLS:
        name = _tool_name(schema)
        if tool_family(name) != provider:
            continue
        specs.append(
            {
                "name": name,
                "broker_name": name.replace("__", ".", 1),
                "description": str((schema.get("function") or {}).get("description") or ""),
                "access": tool_access(name),
                "always_approval": name in ALWAYS_APPROVAL_TOOL_NAMES,
            }
        )
    return specs
