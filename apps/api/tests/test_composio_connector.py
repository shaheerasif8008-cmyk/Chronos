"""Tests for the Composio managed-auth connector path.

Covers the SDK-isolation wrapper (composio_client), the broker-facing connector
(composio_connector), and the ToolBroker route dispatch via the "composio"
sentinel. The Composio SDK is never imported here — every SDK boundary is
monkeypatched, so these run without composio-core installed.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from connectors import composio_client
from connectors.composio_connector import composio_connector
from core.models import AgentContext, ToolResult


def _agent() -> AgentContext:
    return AgentContext(id="task:1", org_id="acme", member_id="user-1", task_id="1")


# ---------------------------------------------------------------------------
# composio_client
# ---------------------------------------------------------------------------

def test_is_composio_provider_covers_saas_not_native():
    assert composio_client.is_composio_provider("gmail")
    assert composio_client.is_composio_provider("slack")
    assert composio_client.is_composio_provider("github")
    # Native/local tools and dedicated connectors never route through Composio.
    for native in ("browser", "fs", "code", "computer", "canva", "mcp"):
        assert not composio_client.is_composio_provider(native)


def test_entity_id_member_vs_org_scope(monkeypatch):
    monkeypatch.setattr(composio_client.settings, "composio_entity_scope", "member")
    assert composio_client.entity_id("acme", "user-1") == "acme:user-1"
    monkeypatch.setattr(composio_client.settings, "composio_entity_scope", "org")
    assert composio_client.entity_id("acme", "user-1") == "org:acme"
    # A missing member falls back to the org entity even in member scope.
    monkeypatch.setattr(composio_client.settings, "composio_entity_scope", "member")
    assert composio_client.entity_id("acme", None) == "org:acme"


def test_resolve_action_maps_known_gmail_tools():
    slug, params = composio_client.resolve_action(
        "gmail.draft", {"to": "a@b.com", "subject": "Hi", "body": "Yo", "cc": "c@d.com"}
    )
    assert slug == "GMAIL_CREATE_EMAIL_DRAFT"
    assert params["recipient_email"] == "a@b.com"
    assert params["subject"] == "Hi"
    assert params["cc"] == ["c@d.com"]

    slug, params = composio_client.resolve_action("gmail.search", {"query": "from:x", "max_results": 5})
    assert slug == "GMAIL_FETCH_EMAILS"
    assert params == {"query": "from:x", "max_results": 5}


def test_resolve_action_maps_managed_slack_github_and_google_drive_tools():
    slug, params = composio_client.resolve_action(
        "slack.send",
        {"channel": "C123", "text": "standup notes", "thread_ts": "170000.1"},
    )
    assert slug == "SLACK_CHAT_POST_MESSAGE"
    assert params == {"channel": "C123", "text": "standup notes", "thread_ts": "170000.1"}

    slug, params = composio_client.resolve_action(
        "slack.read",
        {"channel": "C123", "limit": "5"},
    )
    assert slug == "SLACK_FETCH_CONVERSATION_HISTORY"
    assert params == {"channel": "C123", "limit": 5}

    slug, params = composio_client.resolve_action("github.create_issue", {
        "owner": "acme",
        "repo": "app",
        "title": "Bug",
        "body": "Steps",
        "labels": "bug",
    })
    assert slug == "GITHUB_CREATE_AN_ISSUE"
    assert params == {"owner": "acme", "repo": "app", "title": "Bug", "body": "Steps", "labels": ["bug"]}

    slug, params = composio_client.resolve_action("github.read", {
        "owner": "acme",
        "repo": "app",
        "path": "README.md",
        "ref": "main",
    })
    assert slug == "GITHUB_GET_REPOSITORY_CONTENT"
    assert params == {"owner": "acme", "repo": "app", "path": "README.md", "ref": "main"}

    slug, params = composio_client.resolve_action("google_drive.search", {
        "query": "name contains 'roadmap'",
        "max_results": "3",
    })
    assert slug == "GOOGLEDRIVE_FIND_FILE"
    assert params == {"query": "name contains 'roadmap'", "page_size": 3}

    slug, params = composio_client.resolve_action("google_drive.read", {"file_id": "file-1"})
    assert slug == "GOOGLEDRIVE_GET_FILE_METADATA"
    assert params == {"file_id": "file-1"}


def test_resolve_action_generic_passthrough_is_limited_to_api_tool():
    slug, params = composio_client.resolve_action(
        "slack.api",
        {"composio_action": "SLACK_SEND_MESSAGE", "channel": "#general", "text": "hi", "__org_id": "acme"},
    )
    assert slug == "SLACK_SEND_MESSAGE"
    # Internal (__) and routing keys are stripped; real params pass through.
    assert params == {"channel": "#general", "text": "hi"}


def test_resolve_action_generic_passthrough_flattens_params_object():
    slug, params = composio_client.resolve_action(
        "notion.api",
        {
            "composio_action": "NOTION_SEARCH",
            "params": {"query": "roadmap", "page_size": 5},
            "__member_id": "user-1",
        },
    )
    assert slug == "NOTION_SEARCH"
    assert params == {"query": "roadmap", "page_size": 5}


def test_resolve_action_raises_without_mapping_or_explicit_action():
    with pytest.raises(ValueError):
        composio_client.resolve_action("notion.search", {"query": "roadmap"})


def test_composio_tools_are_visible_to_chat_and_map_to_broker_names():
    from runtime.tool_registry import ALL_TOOLS, INLINE_CHAT_TOOLS, SUBAGENT_TOOLS, to_broker_name, tool_name

    expected = {
        "gmail__read_inbox",
        "gmail__draft",
        "gmail__send",
        "gmail__search",
        "slack__send",
        "slack__read",
        "slack__search",
        "github__create_issue",
        "github__read",
        "github__search",
        "google_drive__search",
        "google_drive__read",
        "google_drive__upload",
        "google_calendar__api",
        "notion__api",
        "linear__api",
        "hubspot__api",
        "airtable__api",
        "jira__api",
        "outlook__api",
        "teams__api",
        "sharepoint_onedrive__api",
        "salesforce__api",
        "stripe__api",
    }

    assert expected <= {tool_name(tool) for tool in ALL_TOOLS}
    assert expected <= {tool_name(tool) for tool in SUBAGENT_TOOLS}
    assert expected <= {tool_name(tool) for tool in INLINE_CHAT_TOOLS}
    assert to_broker_name("slack__send") == "slack.send"
    assert to_broker_name("google_calendar__api") == "google_calendar.api"


def test_composio_write_tools_keep_human_approval_floor():
    from core.tool_broker import _ALWAYS_APPROVAL_TOOLS
    from runtime.tool_registry import ALWAYS_APPROVAL_TOOL_NAMES

    assert {"gmail__send", "slack__send", "github__create_issue", "google_drive__upload"} <= ALWAYS_APPROVAL_TOOL_NAMES
    assert {"gmail.send", "slack.send", "github.create_issue", "google_drive.upload"} <= _ALWAYS_APPROVAL_TOOLS


def test_managed_vault_ref_is_provider_and_entity_scoped():
    ref = composio_client.managed_vault_ref("slack", "acme:user-1")
    assert ref == "composio:slack:acme:user-1"
    assert composio_client.parse_managed_vault_ref(ref) == ("slack", "acme:user-1")
    assert composio_client.parse_managed_vault_ref("composio:acme:user-1") is None


def test_managed_connector_id_matches_entity_scope(monkeypatch):
    monkeypatch.setattr(composio_client.settings, "composio_entity_scope", "member")
    assert composio_client.managed_connector_id("slack", "acme", "user-1") == "slack:acme:user-1"
    monkeypatch.setattr(composio_client.settings, "composio_entity_scope", "org")
    assert composio_client.managed_connector_id("slack", "acme", "user-1") == "slack:acme"


@pytest.mark.asyncio
async def test_execute_action_uses_current_composio_tools_client(monkeypatch):
    calls = {}

    class FakeTools:
        def execute(self, slug, arguments, *, user_id):
            calls["execute"] = {"slug": slug, "arguments": arguments, "user_id": user_id}
            return {"ok": True}

    class FakeComposio:
        def __init__(self, *, api_key):
            calls["api_key"] = api_key
            self.tools = FakeTools()

    monkeypatch.setattr(composio_client.settings, "composio_api_key", "test-key")
    monkeypatch.setitem(sys.modules, "composio", SimpleNamespace(Composio=FakeComposio))
    composio_client._toolset.cache_clear()

    result = await composio_client.execute_action(
        "SLACK_SEARCH_MESSAGES",
        {"query": "from:shaheer"},
        entity="acme:user-1",
    )

    assert result == {"ok": True}
    assert calls == {
        "api_key": "test-key",
        "execute": {
            "slug": "SLACK_SEARCH_MESSAGES",
            "arguments": {"query": "from:shaheer"},
            "user_id": "acme:user-1",
        },
    }


@pytest.mark.asyncio
async def test_initiate_connection_uses_current_connected_account_flow(monkeypatch):
    calls = {}

    class FakeToolkits:
        def _get_auth_config_id(self, *, toolkit):
            calls["toolkit"] = toolkit
            return "ac_slack"

    class FakeConnectedAccounts:
        def initiate(self, *, user_id, auth_config_id, callback_url):
            calls["initiate"] = {
                "user_id": user_id,
                "auth_config_id": auth_config_id,
                "callback_url": callback_url,
            }
            return SimpleNamespace(redirect_url="https://connect.example", connected_account_id="ca_123")

    class FakeComposio:
        def __init__(self, *, api_key):
            self.toolkits = FakeToolkits()
            self.connected_accounts = FakeConnectedAccounts()

    monkeypatch.setattr(composio_client.settings, "composio_api_key", "test-key")
    monkeypatch.setitem(sys.modules, "composio", SimpleNamespace(Composio=FakeComposio))
    composio_client._toolset.cache_clear()

    result = await composio_client.initiate_connection(
        "slack",
        entity="acme:user-1",
        redirect_url="http://localhost:8000/connectors/slack/composio-callback?state=s1",
    )

    assert result == {"redirect_url": "https://connect.example", "connection_id": "ca_123"}
    assert calls == {
        "toolkit": "slack",
        "initiate": {
            "user_id": "acme:user-1",
            "auth_config_id": "ac_slack",
            "callback_url": "http://localhost:8000/connectors/slack/composio-callback?state=s1",
        },
    }


@pytest.mark.asyncio
async def test_connector_health_reports_composio_managed_setup_for_core_saas(monkeypatch):
    from core import connector_health

    async def no_playwright():
        return False, "Playwright is not installed."

    async def verified_composio():
        return connector_health.ProbeResult(
            ok=True,
            checked_at=connector_health._utcnow(),
            latency_ms=8,
        )

    monkeypatch.setattr(connector_health.settings, "composio_api_key", "cmp-test")
    monkeypatch.setattr(composio_client, "is_configured", lambda: True)
    monkeypatch.setattr(connector_health, "_playwright_available", no_playwright)
    monkeypatch.setattr(connector_health, "_probe_composio", verified_composio)
    connector_health._CACHE = None

    health = await connector_health.check_connectors(refresh=True)

    for provider in ("gmail", "slack", "github", "google_drive"):
        assert health[provider]["tier"] == "live"
        assert health[provider]["status"] == "verified"
        assert health[provider]["verified"] is True
        assert health[provider]["auth"] == "composio_managed"
        assert "Composio managed auth" in health[provider]["reason"]
        assert "Connect" in health[provider]["setup"]


# ---------------------------------------------------------------------------
# composio_connector
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connector_returns_demo_when_not_configured(monkeypatch):
    monkeypatch.setattr(composio_client, "is_configured", lambda: False)
    result = await composio_connector.execute(
        "gmail.draft", {"to": "a@b.com", "__connector_tier": "live"}, _agent()
    )
    assert isinstance(result, ToolResult)
    assert result.data.get("demo") is True
    assert "COMPOSIO_API_KEY is not set" in result.data["reason"]


@pytest.mark.asyncio
async def test_connector_demo_tier_short_circuits(monkeypatch):
    monkeypatch.setattr(composio_client, "is_configured", lambda: True)
    result = await composio_connector.execute(
        "slack.api", {"composio_action": "X", "__connector_tier": "demo"}, _agent()
    )
    assert result.data.get("demo") is True


@pytest.mark.asyncio
async def test_connector_executes_and_normalises_success(monkeypatch):
    captured = {}

    async def fake_execute(action, params, *, entity):
        captured.update(action=action, params=params, entity=entity)
        return {"successful": True, "data": {"draft_id": "d-123"}}

    monkeypatch.setattr(composio_client, "is_configured", lambda: True)
    monkeypatch.setattr(composio_client.settings, "composio_entity_scope", "member")
    monkeypatch.setattr(composio_client, "execute_action", fake_execute)

    result = await composio_connector.execute(
        "gmail.draft",
        {"to": "a@b.com", "subject": "Hi", "body": "Yo", "__connector_tier": "live", "__org_id": "acme"},
        _agent(),
    )
    assert result.data == {"draft_id": "d-123"}
    assert captured["action"] == "GMAIL_CREATE_EMAIL_DRAFT"
    assert captured["entity"] == "acme:user-1"


@pytest.mark.asyncio
async def test_connector_executes_slack_mapping_without_explicit_composio_action(monkeypatch):
    captured = {}

    async def fake_execute(action, params, *, entity):
        captured.update(action=action, params=params, entity=entity)
        return {"successful": True, "data": {"ok": True, "channel": params["channel"]}}

    monkeypatch.setattr(composio_client, "is_configured", lambda: True)
    monkeypatch.setattr(composio_client, "execute_action", fake_execute)

    result = await composio_connector.execute(
        "slack.send",
        {"channel": "C123", "text": "hi", "__connector_tier": "live", "__org_id": "acme"},
        _agent(),
    )

    assert result.data == {"ok": True, "channel": "C123"}
    assert captured == {
        "action": "SLACK_CHAT_POST_MESSAGE",
        "params": {"channel": "C123", "text": "hi"},
        "entity": "acme:user-1",
    }


@pytest.mark.asyncio
async def test_connector_reports_failure_response(monkeypatch):
    async def fake_execute(action, params, *, entity):
        return {"successful": False, "error": "boom"}

    monkeypatch.setattr(composio_client, "is_configured", lambda: True)
    monkeypatch.setattr(composio_client, "execute_action", fake_execute)

    result = await composio_connector.execute(
        "gmail.search", {"query": "x", "__connector_tier": "live"}, _agent()
    )
    assert "boom" in result.data["error"]
    assert "failed" in result.summary


@pytest.mark.asyncio
async def test_connector_handles_sdk_exception(monkeypatch):
    async def fake_execute(action, params, *, entity):
        raise RuntimeError("network down")

    monkeypatch.setattr(composio_client, "is_configured", lambda: True)
    monkeypatch.setattr(composio_client, "execute_action", fake_execute)

    result = await composio_connector.execute(
        "gmail.read_inbox", {"__connector_tier": "live"}, _agent()
    )
    assert "network down" in result.data["error"]


# ---------------------------------------------------------------------------
# Broker route dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_dispatches_composio_sentinel(monkeypatch):
    from core import tool_broker

    async def fake_execute(tool, args, agent):
        return ToolResult(data={"routed": tool, "entity_ok": args.get("__org_id")}, summary="routed")

    import connectors.composio_connector as cc
    monkeypatch.setattr(cc.composio_connector, "execute", fake_execute)

    result = await tool_broker._route(_agent(), "slack.api", {"text": "hi"}, vault_ref="composio", tier="live")
    assert result.data["routed"] == "slack.api"


@pytest.mark.asyncio
async def test_broker_requires_active_managed_connector_for_composio_live_call(monkeypatch):
    from core import tool_broker
    from core.exceptions import ConnectorNotFound
    import connectors.registry as registry

    async def fake_noop(*args, **kwargs):
        return None

    async def fake_allow(*args, **kwargs):
        return True

    async def fake_policy(*args, **kwargs):
        return {"enabled": True}

    async def fake_autonomy(*args, **kwargs):
        return "supervised"

    async def fake_gate(*args, **kwargs):
        return SimpleNamespace(allow=True, reason="")

    async def fake_overrides(*args, **kwargs):
        return {}

    async def fake_level(*args, **kwargs):
        return SimpleNamespace(successes=3)

    async def fake_degraded(*args, **kwargs):
        return None

    async def fake_tier(*args, **kwargs):
        return "live"

    async def fake_route(agent, tool, args, vault_ref, tier="live"):
        return ToolResult(data={"vault_ref": vault_ref, "tier": tier}, summary="routed")

    monkeypatch.setattr(tool_broker.permissions, "check", fake_allow)
    monkeypatch.setattr(tool_broker, "_check_rate_limit", fake_noop)
    monkeypatch.setattr(tool_broker, "_check_loop", fake_noop)
    monkeypatch.setattr(tool_broker, "tool_policy", fake_policy)
    monkeypatch.setattr(tool_broker, "workspace_autonomy", fake_autonomy)
    monkeypatch.setattr(tool_broker.risk_registry, "get_overrides", fake_overrides)
    monkeypatch.setattr(tool_broker.autonomy, "evaluate", fake_gate)
    monkeypatch.setattr(tool_broker.trust, "get_trust_level", fake_level)
    monkeypatch.setattr(tool_broker.trust, "novelty_from_successes", lambda successes: 0.0)
    monkeypatch.setattr(tool_broker.trust, "record_outcome", fake_noop)
    monkeypatch.setattr(tool_broker.audit, "log", fake_noop)
    monkeypatch.setattr(tool_broker, "connector_tier", fake_tier)
    monkeypatch.setattr(tool_broker, "degraded_note", fake_degraded)
    monkeypatch.setattr(tool_broker, "_route", fake_route)
    monkeypatch.setattr(composio_client, "is_configured", lambda: True)
    monkeypatch.setattr(composio_client.settings, "composio_entity_scope", "member")

    async def wrong_connector(agent, tool):
        return SimpleNamespace(vault_ref="composio:slack:acme:someone-else")

    monkeypatch.setattr(registry, "get", wrong_connector)
    with pytest.raises(ConnectorNotFound):
        await tool_broker.tool_broker.execute(_agent(), "slack.read", {"channel": "C123"})

    async def matching_connector(agent, tool):
        return SimpleNamespace(vault_ref="composio:slack:acme:user-1")

    monkeypatch.setattr(registry, "get", matching_connector)
    result = await tool_broker.tool_broker.execute(_agent(), "slack.read", {"channel": "C123"})
    assert result.data == {"vault_ref": "composio", "tier": "live"}
