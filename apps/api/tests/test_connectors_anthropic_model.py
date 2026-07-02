"""
Proofs for the Anthropic-style connectors model.

- Composio managed auth makes catalog apps configured (one-click Connect) with
  no per-provider OAuth env vars.
- The model's tool list only includes SaaS connector tools that are actually
  connected (or running in demo/fixture tier), never blocked tools.
- The system prompt always carries a Connectors block: connected apps,
  available-to-connect apps, and registered custom MCP servers.
- Per-tool permissions round-trip through the settings store.
"""
from __future__ import annotations

import pytest


# ── Catalog: Composio managed auth = configured ──────────────────────────────

def test_catalog_marks_composio_providers_configured(monkeypatch):
    from connectors import composio_client, oauth_apps

    monkeypatch.setattr(composio_client, "is_configured", lambda: True)
    apps = {app["id"]: app for app in oauth_apps.available_apps()}

    gmail = apps["gmail"]
    assert gmail["configured"] is True
    assert gmail["auth_mode"] == "composio"

    # Non-Composio integrations keep the env-credential requirement.
    webhooks = apps["webhooks"]
    assert webhooks["auth_mode"] in {"direct", "unconfigured"}


def test_catalog_unconfigured_without_composio_or_env(monkeypatch):
    from connectors import composio_client, oauth_apps

    monkeypatch.setattr(composio_client, "is_configured", lambda: False)
    monkeypatch.setattr(oauth_apps, "_env_value", lambda name: "")
    apps = {app["id"]: app for app in oauth_apps.available_apps()}
    assert apps["notion"]["configured"] is False
    assert apps["notion"]["auth_mode"] == "unconfigured"


# ── Tool resolution: connected connectors shape what the model sees ──────────

@pytest.mark.asyncio
async def test_resolver_hides_unconnected_live_saas_tools(monkeypatch):
    import core.connector_tools as ct

    async def no_connections(org_id):
        return {}

    async def live_tier(provider):
        return "live"

    monkeypatch.setattr(ct, "connected_providers", no_connections)
    monkeypatch.setattr("core.connector_health.connector_tier", live_tier)

    from runtime.tool_registry import GMAIL_SEARCH, BROWSER_SEARCH

    resolved = await ct.resolve_agent_tools([GMAIL_SEARCH, BROWSER_SEARCH], org_id="default")
    names = {t["function"]["name"] for t in resolved}
    assert "browser__search" in names        # native tools always survive
    assert "gmail__search" not in names      # live-capable but not connected


@pytest.mark.asyncio
async def test_resolver_keeps_connected_and_demo_tier_tools(monkeypatch):
    import core.connector_tools as ct

    async def gmail_connected(org_id):
        return {"gmail": "user@example.com"}

    async def live_tier(provider):
        return "live"

    monkeypatch.setattr(ct, "connected_providers", gmail_connected)
    monkeypatch.setattr("core.connector_health.connector_tier", live_tier)

    from runtime.tool_registry import GMAIL_SEARCH, SLACK_SEARCH

    resolved = await ct.resolve_agent_tools([GMAIL_SEARCH, SLACK_SEARCH], org_id="default")
    names = {t["function"]["name"] for t in resolved}
    assert "gmail__search" in names          # connected
    assert "slack__search" not in names      # not connected

    # Demo/fixture tier keeps tools so local dev without credentials still works.
    async def demo_tier(provider):
        return "demo"

    monkeypatch.setattr("core.connector_health.connector_tier", demo_tier)
    resolved = await ct.resolve_agent_tools([SLACK_SEARCH], org_id="default")
    assert {t["function"]["name"] for t in resolved} == {"slack__search"}


@pytest.mark.asyncio
async def test_resolver_removes_blocked_and_disabled_tools(monkeypatch):
    import core.connector_tools as ct

    async def gmail_connected(org_id):
        return {"gmail": "user@example.com"}

    async def blocked_gmail_send(org_id):
        return {"gmail.send": "blocked"}

    monkeypatch.setattr(ct, "connected_providers", gmail_connected)
    monkeypatch.setattr("core.settings_store.tool_permissions", blocked_gmail_send)

    from runtime.tool_registry import BROWSER_SEARCH, GMAIL_SEARCH, GMAIL_SEND

    resolved = await ct.resolve_agent_tools(
        [BROWSER_SEARCH, GMAIL_SEARCH, GMAIL_SEND],
        org_id="default",
        disabled_tools=["browser"],
    )
    names = {t["function"]["name"] for t in resolved}
    assert "gmail__send" not in names        # blocked by tool permissions
    assert "browser__search" not in names    # disabled for this conversation
    assert "gmail__search" in names


# ── System prompt: the model always knows the connectors system exists ───────

@pytest.mark.asyncio
async def test_connectors_prompt_block_lists_connected_and_available(monkeypatch):
    import core.connector_tools as ct

    async def gmail_connected(org_id):
        return {"gmail": "user@example.com"}

    async def one_server(org_id):
        return [{"id": "srv1", "name": "Internal KB", "status": "available"}]

    monkeypatch.setattr(ct, "connected_providers", gmail_connected)
    monkeypatch.setattr(ct, "registered_mcp_servers", one_server)

    block = await ct.connectors_prompt_block("default")
    assert block.startswith("# Connectors")
    assert "Gmail (user@example.com)" in block
    assert "Notion" in block                        # available but not connected
    assert "Settings → Connectors" in block         # tells model where to send the user
    assert "Internal KB" in block                   # custom MCP server surfaced
    assert "platform__invoke" in block


# ── Per-tool permissions: settings-store round trip + validation ─────────────

@pytest.mark.asyncio
async def test_tool_permission_round_trip():
    from core.config import settings as cfg
    from core.models import Member
    from core.settings_store import set_tool_permission, tool_permissions

    member = Member(
        id="member-perm-test", organization_id="default", region=cfg.region,
        email="perm@test.local", role="admin",
    )
    updated = await set_tool_permission(member, "gmail.send", "blocked")
    assert updated["gmail.send"] == "blocked"
    assert (await tool_permissions("default"))["gmail.send"] == "blocked"

    # Reset to default so other tests see a clean slate.
    await set_tool_permission(member, "gmail.send", "default")

    with pytest.raises(ValueError):
        await set_tool_permission(member, "gmail.send", "sometimes")


# ── End-to-end over HTTP: catalog → permissions → connected state ────────────

@pytest.mark.asyncio
async def test_connectors_flow_over_http(monkeypatch):
    import uuid

    import httpx

    import main
    from connectors import composio_client
    from core.auth import create_access_token
    from core.db import engine, reflect_table

    # Composio managed auth "configured" — the exact scenario the catalog must
    # report as ready-to-connect without per-provider OAuth env vars.
    monkeypatch.setattr(composio_client, "is_configured", lambda: True)

    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    connectors_t = await reflect_table("connectors")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"o-{org_id[:8]}", name="O"))
        await conn.execute(
            members.insert().values(
                id=member_id, organization_id=org_id, email=f"{member_id[:8]}@t.io", role="admin"
            )
        )
        # A connected Slack account for this org.
        await conn.execute(
            connectors_t.insert().values(
                id=f"slack:{org_id}:{member_id}",
                organization_id=org_id,
                provider="slack",
                account_handle="acme-workspace",
                vault_ref=f"composio:slack:{org_id}:{member_id}",
                status="active",
                scopes=["chat:write"],
            )
        )

    auth = {"Authorization": f"Bearer {create_access_token(member_id)}"}
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Catalog: Composio-managed providers are configured (one-click
        #    Connect), the seeded Slack row shows connected, tools carry specs.
        catalog = await client.get("/connectors/catalog", headers=auth)
        assert catalog.status_code == 200, catalog.text
        apps = {app["id"]: app for app in catalog.json()}
        assert apps["gmail"]["configured"] is True
        assert apps["gmail"]["auth_mode"] == "composio"
        assert apps["slack"]["connected"] is True
        assert apps["slack"]["account_handle"] == "acme-workspace"
        slack_tools = {t["name"]: t for t in apps["slack"]["tools"]}
        assert "slack__send" in slack_tools and "slack__search" in slack_tools
        assert slack_tools["slack__search"]["permission"] == "default"

        # 2. Set a per-tool permission and see it reflected everywhere.
        put = await client.put(
            "/connectors/tool-permissions/slack__search",
            json={"permission": "blocked"},
            headers=auth,
        )
        assert put.status_code == 200, put.text
        perms = await client.get("/connectors/tool-permissions", headers=auth)
        assert perms.json()["slack.search"] == "blocked"
        catalog2 = await client.get("/connectors/catalog", headers=auth)
        apps2 = {app["id"]: app for app in catalog2.json()}
        assert {t["name"]: t for t in apps2["slack"]["tools"]}["slack__search"]["permission"] == "blocked"

    # 3. The model's tool list for this org: connected Slack tools present,
    #    except the blocked one; unconnected live SaaS providers hidden.
    import core.connector_tools as ct

    async def live_tier(provider):
        return "live"

    monkeypatch.setattr("core.connector_health.connector_tier", live_tier)
    from runtime.tool_registry import GMAIL_SEARCH, SLACK_SEARCH, SLACK_READ

    resolved = await ct.resolve_agent_tools(
        [GMAIL_SEARCH, SLACK_SEARCH, SLACK_READ], org_id=org_id
    )
    names = {t["function"]["name"] for t in resolved}
    assert names == {"slack__read"}

    # 4. And the system prompt block knows about all of it.
    block = await ct.connectors_prompt_block(org_id)
    assert "Slack (acme-workspace)" in block
    assert "Gmail" in block  # listed as available to connect


def test_provider_tool_specs_classify_read_write():
    from core.connector_tools import provider_tool_specs

    specs = {spec["name"]: spec for spec in provider_tool_specs("gmail")}
    assert specs["gmail__search"]["access"] == "read"
    assert specs["gmail__send"]["access"] == "write"
    assert specs["gmail__send"]["always_approval"] is True
    assert specs["gmail__send"]["broker_name"] == "gmail.send"
