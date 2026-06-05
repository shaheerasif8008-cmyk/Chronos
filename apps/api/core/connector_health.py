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
    if settings.tavily_api_key:
        return True, "Tavily API key is configured; browser.search uses Tavily for live web search."
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

    gmail_live = bool(settings.google_client_id and settings.google_client_secret)
    gmail_status = "live" if gmail_live else "demo"
    gmail_reason = (
        "Google OAuth2 configured; each user must authorise via Connect."
        if gmail_live
        else "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set; Gmail drafts use local demo storage."
    )

    browser_ok, browser_reason = await _browser_available()
    browser_status = "live" if browser_ok else "fixture"

    health = {
        "gmail": {
            "status": gmail_status,
            "tier": gmail_status,
            "reason": gmail_reason,
            "setup": None if gmail_status == "live" else "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        },
        "browser": {
            "status": browser_status,
            "tier": browser_status,
            "reason": browser_reason,
            "setup": None if browser_status == "live" else "Set TAVILY_API_KEY or run: pip install playwright && playwright install chromium",
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
        "repo": {
            "status": "live",
            "tier": "live",
            "reason": "Bundled fixture repo workspaces are available with branch, file, pytest, and diff tools inside the task workspace jail.",
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
