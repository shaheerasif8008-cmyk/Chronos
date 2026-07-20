from __future__ import annotations

import pytest


def test_runtime_tool_installer_only_supports_allowlisted_tools():
    from core.tool_installer import supported_runtime_tools

    assert "playwright.chromium" in supported_runtime_tools()
    assert "apt.curl" not in supported_runtime_tools()


@pytest.mark.asyncio
async def test_runtime_tool_installer_rejects_unknown_tool():
    from core.tool_installer import ensure_runtime_tool

    with pytest.raises(ValueError):
        await ensure_runtime_tool(
            "pip.anything",
            organization_id="org-installer-test",
            reason="test",
        )


@pytest.mark.asyncio
async def test_browser_operator_installs_missing_chromium_and_retries(monkeypatch):
    from connectors import browser_operator as browser_module
    from connectors.browser_operator import BrowserOperator
    from core.tool_installer import ToolInstallResult

    monkeypatch.setattr(browser_module.settings, "browserbase_operator_enabled", False)
    operator = BrowserOperator()
    calls = {"launch": 0, "install": 0}
    session = {
        "id": "browser-runtime-install-test",
        "organization_id": "org-browser-install",
        "status": "active",
        "storage_state": {},
    }

    async def fake_api(_session):
        return object()

    async def fake_launch(_session, _api):
        calls["launch"] += 1
        if calls["launch"] == 1:
            raise RuntimeError("Executable doesn't exist. Please run the following command: playwright install")
        return "page-ok"

    async def fake_install(tool, *, organization_id, reason, actor_id="chronos"):
        calls["install"] += 1
        assert tool == "playwright.chromium"
        assert organization_id == "org-browser-install"
        assert reason == "browser runtime missing chromium"
        return ToolInstallResult(tool=tool, status="installed", returncode=0, reason=reason)

    async def noop_save(_session):
        return None

    async def noop_event(_session, _event_type, _payload):
        return None

    monkeypatch.setattr(operator, "_playwright_api_or_none", fake_api)
    monkeypatch.setattr(operator, "_launch_page", fake_launch)
    monkeypatch.setattr(operator, "_save_session", noop_save)
    monkeypatch.setattr(operator, "_record_event", noop_event)
    monkeypatch.setattr(browser_module, "ensure_runtime_tool", fake_install)

    page = await operator._runtime_page(session)

    assert page == "page-ok"
    assert calls == {"launch": 2, "install": 1}


@pytest.mark.asyncio
async def test_browser_operator_degrades_when_chromium_install_fails(monkeypatch):
    from connectors import browser_operator as browser_module
    from connectors.browser_operator import BrowserOperator, _MetadataOnlyPage
    from core.tool_installer import ToolInstallResult

    monkeypatch.setattr(browser_module.settings, "browserbase_operator_enabled", False)
    operator = BrowserOperator()
    session = {
        "id": "browser-runtime-install-fail-test",
        "organization_id": "org-browser-install",
        "status": "active",
        "storage_state": {},
    }

    async def fake_api(_session):
        return object()

    async def fake_launch(_session, _api):
        raise RuntimeError("Executable doesn't exist. Please run the following command: playwright install")

    async def fake_install(tool, *, organization_id, reason, actor_id="chronos"):
        return ToolInstallResult(tool=tool, status="failed", returncode=1, stderr="network unavailable", reason=reason)

    async def noop_save(_session):
        return None

    async def noop_event(_session, _event_type, _payload):
        return None

    monkeypatch.setattr(operator, "_playwright_api_or_none", fake_api)
    monkeypatch.setattr(operator, "_launch_page", fake_launch)
    monkeypatch.setattr(operator, "_save_session", noop_save)
    monkeypatch.setattr(operator, "_record_event", noop_event)
    monkeypatch.setattr(browser_module, "ensure_runtime_tool", fake_install)

    page = await operator._runtime_page(session)

    assert isinstance(page, _MetadataOnlyPage)
    assert session["status"] == "degraded"
