"""Tests for the Composio managed-auth connector path.

Covers the SDK-isolation wrapper (composio_client), the broker-facing connector
(composio_connector), and the ToolBroker route dispatch via the "composio"
sentinel. The Composio SDK is never imported here — every SDK boundary is
monkeypatched, so these run without composio-core installed.
"""
from __future__ import annotations

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


def test_resolve_action_generic_passthrough_with_explicit_action():
    slug, params = composio_client.resolve_action(
        "slack.api",
        {"composio_action": "SLACK_SENDS_A_MESSAGE", "channel": "#general", "text": "hi", "__org_id": "acme"},
    )
    assert slug == "SLACK_SENDS_A_MESSAGE"
    # Internal (__) and routing keys are stripped; real params pass through.
    assert params == {"channel": "#general", "text": "hi"}


def test_resolve_action_raises_without_mapping_or_explicit_action():
    with pytest.raises(ValueError):
        composio_client.resolve_action("notion.search", {"query": "roadmap"})


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
