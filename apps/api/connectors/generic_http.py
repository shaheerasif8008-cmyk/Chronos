"""
Generic HTTP connector — makes authenticated API calls for any OAuth2-connected app.

Tool name format: {provider}.api
Args:
  method    — GET / POST / PUT / PATCH / DELETE
  endpoint  — path relative to app's api_base, e.g. "/v1/search"
  params    — query params dict (optional)
  body      — JSON body dict (optional)

The LLM decides what endpoint to call; we just inject the auth header and
enforce the request through the broker.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from core.models import ToolResult

log = logging.getLogger(__name__)

TOKEN_REFRESH_BUFFER = 300  # seconds


async def _get_token(vault_ref: str, *, org_id: str) -> tuple[str, dict]:
    """Return (access_token, full_creds). Refreshes proactively if near expiry."""
    from connectors.vault import get as vault_get, update as vault_update
    from connectors.oauth_apps import get_app, get_client_credentials
    import os

    creds = await vault_get(vault_ref, org_id=org_id)
    provider = creds.get("provider", "")
    app = get_app(provider)

    refresh_token = creds.get("refresh_token", "")
    expires_at = float(creds.get("expires_at") or 0)
    needs_refresh = refresh_token and app and app.has_refresh and time.time() >= expires_at

    if needs_refresh:
        client_id, client_secret = get_client_credentials(app)
        if not client_id:
            # Try the standard env convention as fallback
            client_id = os.environ.get(f"{provider.upper()}_CLIENT_ID", "")
            client_secret = os.environ.get(f"{provider.upper()}_CLIENT_SECRET", "")

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                app.token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )

        if resp.status_code < 400:
            data = resp.json()
            creds["access_token"] = data["access_token"]
            creds["expires_at"] = str(time.time() + data.get("expires_in", 3600) - TOKEN_REFRESH_BUFFER)
            if data.get("refresh_token"):
                creds["refresh_token"] = data["refresh_token"]
            await vault_update(vault_ref, creds)
        else:
            log.warning("Token refresh failed for %s vault_ref=%r: %s", provider, vault_ref, resp.text[:200])

    return str(creds.get("access_token", "")), creds


async def call(
    vault_ref: str,
    method: str,
    endpoint: str,
    *,
    org_id: str,
    params: dict | None = None,
    body: dict | None = None,
) -> dict[str, Any]:
    """Make one authenticated HTTP call. Returns parsed JSON (or empty dict)."""
    from connectors.oauth_apps import get_app
    from connectors.vault import get as vault_get, update as vault_update

    creds = await vault_get(vault_ref, org_id=org_id)
    provider = creds.get("provider", "")
    app = get_app(provider)
    api_base = app.api_base if app else creds.get("api_base", "")

    token, creds = await _get_token(vault_ref, org_id=org_id)
    url = api_base.rstrip("/") + "/" + endpoint.lstrip("/")
    # Never send the bearer token to an internal/loopback address — the api_base
    # (stored) or endpoint (LLM-controlled) could point at metadata/internal hosts.
    from core.ssrf import assert_safe_url, UnsafeURLError
    try:
        assert_safe_url(url)
    except UnsafeURLError as exc:
        raise RuntimeError(f"{provider} API blocked unsafe URL: {exc}") from exc
    headers = {"Authorization": f"Bearer {token}"}

    # Notion requires a version header
    if provider == "notion":
        headers["Notion-Version"] = "2022-06-28"

    # GitHub wants JSON accept header
    if provider == "github":
        headers["Accept"] = "application/vnd.github+json"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method.upper(),
            url,
            headers=headers,
            params=params,
            json=body,
        )

    if resp.status_code == 401:
        # Force expire and retry once
        creds["expires_at"] = "0"
        from connectors.vault import update as vault_update
        await vault_update(vault_ref, creds)
        token, _ = await _get_token(vault_ref, org_id=org_id)
        headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method.upper(),
                url,
                headers=headers,
                params=params,
                json=body,
            )

    if resp.status_code >= 400:
        raise RuntimeError(
            f"{provider} API {method} {endpoint} → {resp.status_code}: {resp.text[:400]}"
        )

    return resp.json() if resp.content else {}


class GenericHTTPConnector:
    """Connector for all OAuth2 apps that don't have a dedicated connector."""

    async def execute(self, tool: str, args: dict[str, Any], vault_ref: str) -> ToolResult:
        provider = tool.split(".")[0]
        action = tool.split(".", 1)[1] if "." in tool else "api"

        tier = args.pop("__connector_tier", "live")
        org_id = str(args.pop("__org_id", "") or "")
        args.pop("__task_id", None)

        if tier in {"demo", "fixture"}:
            return ToolResult(
                data={"demo": True, "tool": tool},
                summary=f"[demo] {tool} — connect {provider} to use live data",
            )

        method = str(args.get("method", "GET")).upper()
        endpoint = str(args.get("endpoint", "/"))
        params = args.get("params") or args.get("query_params")
        body = args.get("body") or args.get("json")

        try:
            data = await call(vault_ref, method, endpoint, org_id=org_id, params=params, body=body)
        except RuntimeError as exc:
            return ToolResult(data={"error": str(exc)}, summary=f"{tool} failed: {exc}")

        items = _count_items(data)
        summary = f"{provider} {method} {endpoint} → {items}"
        return ToolResult(data=data, summary=summary)


def _count_items(data: dict) -> str:
    for key in ("results", "items", "data", "records", "messages", "channels", "threads"):
        v = data.get(key)
        if isinstance(v, list):
            return f"{len(v)} {key}"
    return "ok"


generic_http_connector = GenericHTTPConnector()
