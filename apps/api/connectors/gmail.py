"""
Gmail connector — direct Google OAuth2 + Gmail REST API.

No third-party connector SDK.  Chronos holds refresh tokens itself (AES-256-GCM
vault) and calls the Gmail API with plain httpx, exactly the way Claude and
ChatGPT plugins work.

Capability map
--------------
  gmail.read_inbox   — list recent INBOX threads
  gmail.search       — search threads by query string
  gmail.draft        — create a draft (NEVER sends automatically)
  gmail.send         — blocked at ToolBroker level (ApprovalRequired)

OAuth state
-----------
  The *state* parameter in the consent URL is an HMAC-SHA256 signed token so
  that the callback can verify it came from us (CSRF) and decode the member_id
  without exposing it in plain-text query strings.

Token lifecycle
---------------
  Vault stores: {refresh_token, access_token, expires_at, scopes, email}
  We proactively refresh when expires_at is within TOKEN_REFRESH_BUFFER_SECONDS.
  If the API returns 401 anyway we refresh once and retry.
"""
from __future__ import annotations

import base64
import email as email_lib
import hashlib
import hmac
import json
import logging
import time
import uuid
from email.mime.text import MIMEText
from typing import Any
from pathlib import Path
from urllib.parse import urlencode

import httpx

from core.exceptions import ApprovalRequired
from core.models import ToolResult

log = logging.getLogger(__name__)

DEMO_DRAFTS_PATH = Path("/tmp/chronos_demo_drafts.jsonl")
TOKEN_REFRESH_BUFFER_SECONDS = 300  # refresh if token expires within 5 minutes

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# ---------------------------------------------------------------------------
# HMAC-signed OAuth state helpers
# ---------------------------------------------------------------------------

def _build_state(member_id: str, org_id: str) -> str:
    """Return a compact signed token encoding member_id + org_id."""
    from core.config import settings

    nonce = uuid.uuid4().hex
    payload = f"{member_id}|{org_id}|{nonce}"
    sig = hmac.new(settings.jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}|{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _verify_state(state: str) -> tuple[str, str]:
    """Verify the HMAC signature and return (member_id, org_id).

    Raises ValueError on any tampering.
    """
    from core.config import settings

    try:
        raw = base64.urlsafe_b64decode(state.encode()).decode()
        member_id, org_id, nonce, received_sig = raw.rsplit("|", 3)
    except Exception as exc:
        raise ValueError("Malformed OAuth state") from exc

    payload = f"{member_id}|{org_id}|{nonce}"
    expected_sig = hmac.new(settings.jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, received_sig):
        raise ValueError("OAuth state signature mismatch — possible CSRF")
    return member_id, org_id


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

async def _refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Exchange a refresh_token for a new access_token via Google's token endpoint."""
    from core.config import settings

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )

    if resp.status_code >= 400:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        raise RuntimeError(f"Token refresh failed: {body.get('error_description') or resp.text}")

    data = resp.json()
    return {
        "access_token": data["access_token"],
        "expires_at": time.time() + data.get("expires_in", 3600) - TOKEN_REFRESH_BUFFER_SECONDS,
    }


async def _get_valid_access_token(vault_ref: str) -> str:
    """Return a valid access_token, refreshing proactively when needed.

    Updates the vault in-place if a refresh is performed.
    """
    from connectors.vault import get as vault_get

    creds = await vault_get(vault_ref)
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(f"vault_ref {vault_ref!r} has no refresh_token — user must reconnect Gmail")

    expires_at = float(creds.get("expires_at") or 0)
    if time.time() < expires_at:
        return str(creds["access_token"])

    # Token expired (or close to it) — refresh
    log.info("Gmail access token near expiry for vault_ref=%r — refreshing", vault_ref)
    updated = await _refresh_access_token(refresh_token)
    creds.update(updated)
    # Overwrite the existing vault entry so vault_ref stays stable (the connectors table
    # keeps pointing at the same vault_ref — no DB update needed).
    from connectors.vault import update as vault_update
    await vault_update(vault_ref, creds)
    return str(updated["access_token"])


# ---------------------------------------------------------------------------
# Gmail REST helpers
# ---------------------------------------------------------------------------

async def _gmail_request(
    method: str,
    path: str,
    access_token: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict[str, Any]:
    """Single Gmail API call.  Returns parsed JSON dict."""
    url = f"{GMAIL_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.request(method, url, headers=headers, params=params, json=json_body)

    if resp.status_code == 401:
        raise _GmailUnauthorised()
    if resp.status_code >= 400:
        raise RuntimeError(f"Gmail API {method} {path} → {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


class _GmailUnauthorised(Exception):
    """Signals that the access token was rejected — triggers a refresh+retry."""


async def _gmail_call_with_refresh(
    vault_ref: str,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict[str, Any]:
    """Call Gmail API; on 401 refresh once and retry."""
    token = await _get_valid_access_token(vault_ref)
    try:
        return await _gmail_request(method, path, token, params=params, json_body=json_body)
    except _GmailUnauthorised:
        log.warning("Gmail 401 for vault_ref=%r — forcing token refresh", vault_ref)
        # Force-expire so _get_valid_access_token will definitely refresh
        from connectors.vault import get as vault_get, update as vault_update
        creds = await vault_get(vault_ref)
        creds["expires_at"] = 0
        await vault_update(vault_ref, creds)
        token = await _get_valid_access_token(vault_ref)
        return await _gmail_request(method, path, token, params=params, json_body=json_body)


def _build_rfc822(to: str, subject: str, body: str, cc: str = "") -> str:
    """Return a base64url-encoded RFC 822 message for the Gmail drafts API."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    raw_bytes = msg.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode().rstrip("=")


# ---------------------------------------------------------------------------
# Main connector class
# ---------------------------------------------------------------------------

class GmailConnector:
    """Routes gmail.* tool calls to the Google Gmail REST API."""

    async def execute(self, tool: str, args: dict[str, Any], vault_ref: str) -> ToolResult:
        from core.connector_health import connector_tier

        if tool == "gmail.send":
            raise ApprovalRequired("gmail.send", "use gmail.draft; sending requires an approval record")

        tier = args.pop("__connector_tier", None) or await connector_tier("gmail")

        from core.config import settings
        if settings.demo_mode or tier in {"demo", "fixture"} or vault_ref in {"demo", "fixture"}:
            return await self._demo_dispatch(tool, args, tier)

        if tool == "gmail.read_inbox":
            return await self._read_inbox(vault_ref, args)
        if tool == "gmail.draft":
            return await self._create_draft(vault_ref, args)
        if tool == "gmail.search":
            return await self._search(vault_ref, args)

        raise ValueError(f"Unknown gmail tool: {tool}")

    # ------------------------------------------------------------------
    # Demo / fixture path
    # ------------------------------------------------------------------

    async def _demo_dispatch(self, tool: str, args: dict, tier: str) -> ToolResult:
        if tool == "gmail.draft":
            return await self._create_demo_draft(args)
        if tool == "gmail.read_inbox":
            return ToolResult(data={"threads": [], "tier": tier}, summary="Demo inbox: 0 threads")
        if tool == "gmail.search":
            return ToolResult(
                data={"threads": [], "tier": tier, "query": args.get("query", "")},
                summary=f"Demo search '{args.get('query', '')}': 0 results",
            )
        raise ValueError(f"Unknown gmail tool: {tool}")

    async def _create_demo_draft(self, args: dict) -> ToolResult:
        DEMO_DRAFTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing: list[dict] = []
        if DEMO_DRAFTS_PATH.exists():
            for line in DEMO_DRAFTS_PATH.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    existing.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("Skipping malformed demo draft line in %s", DEMO_DRAFTS_PATH)
        draft = {
            "id": f"demo-draft-{len(existing) + 1}",
            "to": args.get("to", ""),
            "subject": args.get("subject", ""),
            "body": args.get("body", ""),
            "cc": args.get("cc", ""),
        }
        with DEMO_DRAFTS_PATH.open("a") as fh:
            fh.write(json.dumps(draft) + "\n")
        return ToolResult(
            data={"id": draft["id"], "path": str(DEMO_DRAFTS_PATH)},
            summary=f"Demo draft recorded: {draft['id']}",
        )

    # ------------------------------------------------------------------
    # Live path — Google Gmail REST API
    # ------------------------------------------------------------------

    async def _read_inbox(self, vault_ref: str, args: dict) -> ToolResult:
        max_results = int(args.get("max_results", 10))
        data = await _gmail_call_with_refresh(
            vault_ref,
            "GET",
            "/threads",
            params={"labelIds": "INBOX", "maxResults": max_results},
        )
        threads = data.get("threads") or []
        return ToolResult(
            data=data,
            summary=f"Read inbox: {len(threads)} thread(s)",
        )

    async def _search(self, vault_ref: str, args: dict) -> ToolResult:
        query = args.get("query", "")
        max_results = int(args.get("max_results", 10))
        data = await _gmail_call_with_refresh(
            vault_ref,
            "GET",
            "/threads",
            params={"q": query, "maxResults": max_results},
        )
        threads = data.get("threads") or []
        return ToolResult(
            data=data,
            summary=f"Gmail search '{query}': {len(threads)} result(s)",
        )

    async def _create_draft(self, vault_ref: str, args: dict) -> ToolResult:
        to = args.get("to", "")
        subject = args.get("subject", "")
        body = args.get("body", "")
        cc = args.get("cc", "")

        raw_rfc822 = _build_rfc822(to=to, subject=subject, body=body, cc=cc)
        data = await _gmail_call_with_refresh(
            vault_ref,
            "POST",
            "/drafts",
            json_body={"message": {"raw": raw_rfc822}},
        )
        draft_id = (data.get("id") or data.get("message", {}).get("id") or "unknown")
        return ToolResult(
            data=data,
            summary=f"Draft created: {draft_id}",
        )


gmail_connector = GmailConnector()


# ---------------------------------------------------------------------------
# OAuth helpers — used by the connectors router
# ---------------------------------------------------------------------------

async def oauth_start_url(member_id: str, org_id: str) -> str:
    """Return the Google OAuth2 consent URL to redirect the user to."""
    from core.config import settings

    if not settings.google_client_id:
        raise RuntimeError("GOOGLE_CLIENT_ID is not configured")

    state = _build_state(member_id, org_id)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",   # always ask so we always get a refresh_token
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def oauth_finish(code: str, state: str) -> dict[str, str]:
    """Exchange the authorization code for tokens.

    Returns a credential dict ready to be stored in the vault:
    {refresh_token, access_token, expires_at, scopes, email}
    """
    from core.config import settings

    if not settings.google_client_id or not settings.google_client_secret:
        raise RuntimeError("Google OAuth2 is not configured")

    # Verify state before touching tokens
    member_id, org_id = _verify_state(state)

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if resp.status_code >= 400:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        raise RuntimeError(f"Token exchange failed: {body.get('error_description') or resp.text}")

    data = resp.json()
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            "Google did not return a refresh_token.  "
            "Ensure access_type=offline and prompt=consent were set in the consent URL."
        )

    # Fetch the authenticated user's email address for display purposes
    email = ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            ui = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {data['access_token']}"},
            )
            if ui.status_code == 200:
                email = ui.json().get("email", "")
    except Exception:
        pass  # non-fatal; display only

    return {
        "refresh_token": refresh_token,
        "access_token": data["access_token"],
        "expires_at": str(time.time() + data.get("expires_in", 3600) - TOKEN_REFRESH_BUFFER_SECONDS),
        "scopes": " ".join(GMAIL_SCOPES),
        "email": email,
        "member_id": member_id,
        "org_id": org_id,
    }
