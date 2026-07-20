"""Canva connector — proof harness.

Covers registration, truthful degradation when no Canva account is connected,
and correct request shaping against the Canva Connect REST API (with the HTTP
layer mocked, since CI has no live Canva credentials).
"""
import pytest

from connectors.canva import canva_connector


def test_canva_tools_registered_for_agent_and_inline_use():
    from runtime.tool_registry import ALL_TOOLS, INLINE_CHAT_TOOLS, SUBAGENT_TOOLS, tool_name

    expected = {
        "canva__create_design",
        "canva__list_brand_templates",
        "canva__autofill",
        "canva__export",
        "canva__get_design",
    }
    assert expected <= {tool_name(t) for t in ALL_TOOLS}
    assert expected <= {tool_name(t) for t in SUBAGENT_TOOLS}
    assert expected <= {tool_name(t) for t in INLINE_CHAT_TOOLS}


def test_canva_registered_as_oauth_app():
    from connectors.oauth_apps import get_app

    app = get_app("canva")
    assert app is not None
    assert app.api_base == "https://api.canva.com/rest"
    assert "design:content:write" in app.scopes


@pytest.mark.asyncio
async def test_degrades_truthfully_without_connection(monkeypatch):
    async def _no_connection(_self, _org, _member):
        return None

    monkeypatch.setattr(canva_connector, "_connection_vault_ref", _no_connection.__get__(canva_connector))
    result = await canva_connector.execute(
        "canva.create_design", {"__org_id": "org-x", "title": "ABC"}
    )
    assert result.data["connected"] is False
    assert "not connected" in result.summary.lower()


@pytest.mark.asyncio
async def test_create_design_shapes_request_and_returns_urls(monkeypatch):
    calls: list[tuple] = []
    lookup: list[tuple[str, str | None]] = []

    async def _conn(_self, org, member):
        lookup.append((org, member))
        return "vault:canva:org-x"

    async def _fake_call(self, vault_ref, method, endpoint, *, params=None, body=None):
        calls.append((method, endpoint, body))
        return {
            "design": {
                "id": "DAF123",
                "title": body.get("title"),
                "urls": {"edit_url": "https://canva.com/edit/DAF123", "view_url": "https://canva.com/view/DAF123"},
            }
        }

    monkeypatch.setattr(canva_connector, "_connection_vault_ref", _conn.__get__(canva_connector))
    monkeypatch.setattr(canva_connector, "_call", _fake_call.__get__(canva_connector))

    result = await canva_connector.execute(
        "canva.create_design",
        {
            "__org_id": "org-x",
            "__member_id": "member-a",
            "title": "ABC",
            "design_type": "presentation",
        },
    )
    assert lookup == [("org-x", "member-a")]
    method, endpoint, body = calls[0]
    assert (method, endpoint) == ("POST", "/v1/designs")
    assert body["design_type"] == {"type": "preset", "name": "presentation"}
    assert body["title"] == "ABC"
    assert result.data["design_id"] == "DAF123"
    assert result.data["edit_url"].endswith("/edit/DAF123")


@pytest.mark.asyncio
async def test_export_polls_until_success(monkeypatch):
    async def _conn(_self, _org, _member):
        return "vault:canva:org-x"

    seq = [
        {"job": {"id": "exp1", "status": "in_progress"}},  # POST /v1/exports
        {"job": {"id": "exp1", "status": "in_progress"}},  # first poll
        {"job": {"id": "exp1", "status": "success", "urls": ["https://dl.canva.com/exp1.pptx"]}},
    ]

    async def _fake_call(self, vault_ref, method, endpoint, *, params=None, body=None):
        return seq.pop(0)

    monkeypatch.setattr(canva_connector, "_connection_vault_ref", _conn.__get__(canva_connector))
    monkeypatch.setattr(canva_connector, "_call", _fake_call.__get__(canva_connector))
    # Avoid real sleeps between polls.
    monkeypatch.setattr("connectors.canva._JOB_POLL_DELAY_S", 0)

    result = await canva_connector.execute(
        "canva.export", {"__org_id": "org-x", "design_id": "DAF123", "format": "pptx"}
    )
    assert result.data["status"] == "success"
    assert result.data["download_urls"] == ["https://dl.canva.com/exp1.pptx"]
