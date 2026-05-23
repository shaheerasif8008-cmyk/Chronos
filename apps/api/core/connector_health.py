from __future__ import annotations

import importlib.util
import time
from typing import Any

from core.config import settings


ConnectorHealth = dict[str, dict[str, Any]]
_CACHE: tuple[float, ConnectorHealth] | None = None
_CACHE_TTL_SECONDS = 30.0


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


async def _browser_available() -> tuple[bool, str]:
    if not _module_available("playwright"):
        return False, "playwright is not installed; browser.search uses fixture results."
    try:
        from playwright.async_api import async_playwright  # type: ignore[import]

        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            await browser.close()
        finally:
            await playwright.stop()
    except Exception as exc:
        return False, f"Chromium is not available for Playwright; browser.search uses fixture results. {exc}"
    return True, "Playwright Chromium is available for live browser tools."


async def check_connectors(*, refresh: bool = False) -> ConnectorHealth:
    global _CACHE
    now = time.monotonic()
    if not refresh and _CACHE and now - _CACHE[0] < _CACHE_TTL_SECONDS:
        return _CACHE[1]

    gmail_status = "live" if settings.composio_api_key and _module_available("composio") else "demo"
    gmail_reason = "Composio configured; OAuth is required per user."
    if not settings.composio_api_key:
        gmail_reason = "COMPOSIO_API_KEY is not set; Gmail drafts use local demo storage."
    elif not _module_available("composio"):
        gmail_reason = "composio package is not installed; Gmail drafts use local demo storage."

    browser_ok, browser_reason = await _browser_available()
    browser_status = "live" if browser_ok else "fixture"

    health = {
        "gmail": {
            "status": gmail_status,
            "tier": gmail_status,
            "reason": gmail_reason,
            "setup": None if gmail_status == "live" else "Set COMPOSIO_API_KEY and install composio.",
        },
        "browser": {
            "status": browser_status,
            "tier": browser_status,
            "reason": browser_reason,
            "setup": None if browser_status == "live" else "pip install playwright && playwright install chromium",
        },
        "fs": {
            "status": "live",
            "tier": "live",
            "reason": "Task workspace filesystem tools are available with a per-task path jail.",
            "setup": None,
        },
        "code": {
            "status": "live",
            "tier": "live",
            "reason": "Restricted Python subprocess execution is available with timeout and resource limits.",
            "setup": None,
        },
        "mcp": {
            "status": "available",
            "tier": "live",
            "reason": "MCP servers can be registered and discovered; execution requires a reachable local or remote JSON-RPC MCP server.",
            "setup": "Register an MCP server under Connectors before using mcp.<server_id>.<tool>.",
        },
    }
    _CACHE = (now, health)
    return health


async def connector_tier(provider: str) -> str:
    if settings.demo_mode:
        return "demo"
    health = await check_connectors()
    return str(health.get(provider, {}).get("tier") or "fixture")
