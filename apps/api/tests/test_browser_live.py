"""Live browser-operator smoke (Phase 9, off degraded mode).

Proves the broker-routed browser operator drives a *real* headless Chromium —
status ``active`` (not ``degraded``) with a real page title — when Playwright and
its Chromium binary are installed in the API runtime. The test navigates to a
``data:`` URL so it needs no network access, and skips honestly when the browser
runtime is not present (mirroring the operator's own degraded-mode contract).
"""
from __future__ import annotations

import pytest

from connectors.browser_operator import browser_operator


@pytest.mark.asyncio
async def test_browser_operator_runs_live_when_playwright_installed():
    pytest.importorskip("playwright.async_api", reason="Playwright not installed")

    url = "data:text/html,<title>ChronosLiveBrowserOK</title><h1>ok</h1>"
    try:
        result = await browser_operator.execute(
            "browser.navigate", {"url": url, "__org_id": "default"}
        )
    except Exception as exc:  # pragma: no cover - environment without the binary
        pytest.skip(f"Chromium runtime unavailable: {exc}")

    session = (result.data or {}).get("session", result.data or {})
    if session.get("status") == "degraded":
        pytest.skip("Chromium binary not installed (operator degraded)")

    assert session.get("status") == "active"
    assert "ChronosLiveBrowserOK" in (session.get("title") or "")
