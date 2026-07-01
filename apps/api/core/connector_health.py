from __future__ import annotations

import importlib.util
import time
from typing import Any

from core.config import settings


ConnectorHealth = dict[str, dict[str, Any]]
_CACHE: tuple[float, ConnectorHealth] | None = None
_CACHE_TTL_SECONDS = 30.0

_COMPOSIO_CORE_PROVIDERS = {
    "gmail": "Gmail",
    "slack": "Slack",
    "github": "GitHub",
    "google_drive": "Google Drive",
}


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

    from connectors.composio_client import is_configured as _composio_configured

    composio_on = _composio_configured()
    browser_ok, browser_reason = await _browser_available()
    browser_status = "live" if browser_ok else "fixture"

    health = {
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
    health.update(_managed_saas_health(composio_on=composio_on))
    _CACHE = (now, health)
    return health


def _managed_saas_health(*, composio_on: bool) -> ConnectorHealth:
    if composio_on:
        return {
            provider: {
                "status": "available",
                "tier": "live",
                "auth": "composio_managed",
                "reason": (
                    f"Composio managed auth is configured for {label}; each user still "
                    "needs to connect the app before live actions have account access."
                ),
                "setup": f"Connect {label} in Connectors to bind the current user/entity in Composio.",
            }
            for provider, label in _COMPOSIO_CORE_PROVIDERS.items()
        }

    google_oauth = bool(settings.google_client_id and settings.google_client_secret)
    direct: ConnectorHealth = {
        "gmail": {
            "status": "live" if google_oauth else "demo",
            "tier": "live" if google_oauth else "demo",
            "auth": "direct_oauth",
            "reason": (
                "Google OAuth2 configured; each user must authorise via Connect."
                if google_oauth
                else (
                    "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set and COMPOSIO_API_KEY is not set; "
                    "Gmail drafts use local demo storage."
                )
            ),
            "setup": None if google_oauth else "Set COMPOSIO_API_KEY or GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        },
        "google_drive": {
            "status": "available" if google_oauth else "fixture",
            "tier": "live" if google_oauth else "fixture",
            "auth": "direct_oauth",
            "reason": (
                "Google OAuth2 configured for Drive; each user must authorise via Connect."
                if google_oauth
                else "Google Drive needs COMPOSIO_API_KEY or Google OAuth2 client credentials before live actions can run."
            ),
            "setup": None if google_oauth else "Set COMPOSIO_API_KEY or GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        },
    }

    direct.update(
        {
            "slack": _direct_oauth_health(
                "Slack",
                bool(settings.slack_client_id and settings.slack_client_secret),
                "SLACK_CLIENT_ID and SLACK_CLIENT_SECRET",
            ),
            "github": _direct_oauth_health(
                "GitHub",
                bool(settings.github_client_id and settings.github_client_secret),
                "GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET",
            ),
        }
    )
    return direct


def _direct_oauth_health(label: str, configured: bool, env_label: str) -> dict[str, Any]:
    return {
        "status": "available" if configured else "fixture",
        "tier": "live" if configured else "fixture",
        "auth": "direct_oauth",
        "reason": (
            f"{label} OAuth2 credentials are configured; each user must authorise via Connect."
            if configured
            else f"{label} needs COMPOSIO_API_KEY or {env_label} before live actions can run."
        ),
        "setup": None if configured else f"Set COMPOSIO_API_KEY or {env_label}.",
    }


async def connector_tier(provider: str) -> str:
    if settings.demo_mode:
        return "demo"
    health = await check_connectors()
    return str(health.get(provider, {}).get("tier") or "fixture")


async def degraded_note(provider: str) -> str | None:
    """Return a note when *provider* serves placeholder (non-real) data, else None.

    Keyed off the connector health table so only genuinely degraded providers are
    flagged — e.g. gmail running on local demo storage or browser tools returning
    fixtures. Fully live providers, and providers with no health entry, return
    None. Surfaced to the model so it never treats stub output as real."""
    health = await check_connectors()
    entry = health.get(provider)
    if not entry or str(entry.get("status")) in {"live", "available"}:
        return None
    return str(
        entry.get("reason")
        or f"{provider} is not fully configured and returns placeholder (non-real) results."
    )
