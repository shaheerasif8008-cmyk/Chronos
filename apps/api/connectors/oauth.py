"""Direct per-provider OAuth2 (authorization-code + PKCE) connect flow.

This is the claude.ai-style connector directory mechanism: a catalog of official
providers, each with a "Connect" action that sends the user to the provider's own
sign-in, then a public callback that exchanges the code for tokens, vaults them,
and activates a `connectors` row so the existing connector runtime can use them.

Operational note: each provider needs an OAuth app registered with that provider's
developer console; its client id/secret are read from env (e.g. OAUTH_GOOGLE_CLIENT_ID,
OAUTH_GOOGLE_CLIENT_SECRET). Providers without configured credentials surface in the
directory as not-configured rather than broken — the same constraint Claude has, just
with Anthropic's pre-registered apps.

Security:
  - `state` is HMAC-signed (CSRF + binds the public callback back to a member).
  - PKCE (S256) for providers that support it; the verifier stays server-side in Redis.
  - redirect_uri is pinned server-side, never taken from the request.
  - Tokens go to the vault; only vault_ref is ever logged (RULE 7).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import insert

from connectors import vault
from core.config import settings
from core.db import engine, reflect_table
from core.redis import redis_client

_PKCE_TTL_SECONDS = 600


@dataclass(frozen=True)
class ProviderSpec:
    provider: str
    name: str
    description: str
    authorize_url: str
    token_url: str
    scopes: list[str]
    icon: str = ""
    pkce: bool = True
    extra_authorize_params: dict[str, str] = field(default_factory=dict)

    @property
    def client_id_env(self) -> str:
        return f"OAUTH_{self.provider.upper()}_CLIENT_ID"

    @property
    def client_secret_env(self) -> str:
        return f"OAUTH_{self.provider.upper()}_CLIENT_SECRET"


# A representative catalog covering the common OAuth flavors. Add more by adding a
# ProviderSpec and registering the provider's OAuth app credentials in env.
PROVIDER_CATALOG: dict[str, ProviderSpec] = {
    "google": ProviderSpec(
        provider="google",
        name="Google",
        description="Search, read, and act across Gmail, Drive, and Calendar.",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=[
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/calendar.events",
        ],
        icon="google",
        pkce=True,
        extra_authorize_params={"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
    ),
    "microsoft": ProviderSpec(
        provider="microsoft",
        name="Microsoft 365",
        description="Access SharePoint, OneDrive, Outlook, and Teams.",
        authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        scopes=["offline_access", "openid", "email", "Mail.ReadWrite", "Files.Read.All", "Calendars.ReadWrite"],
        icon="microsoft",
        pkce=True,
    ),
    "slack": ProviderSpec(
        provider="slack",
        name="Slack",
        description="Read channels and post messages on your behalf.",
        authorize_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",
        scopes=["channels:read", "chat:write", "users:read"],
        icon="slack",
        pkce=False,
    ),
    "github": ProviderSpec(
        provider="github",
        name="GitHub",
        description="Read repositories, issues, and pull requests.",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scopes=["repo", "read:org"],
        icon="github",
        pkce=False,
    ),
    "notion": ProviderSpec(
        provider="notion",
        name="Notion",
        description="Search and update pages and databases.",
        authorize_url="https://api.notion.com/v1/oauth/authorize",
        token_url="https://api.notion.com/v1/oauth/token",
        scopes=[],
        icon="notion",
        pkce=False,
        extra_authorize_params={"owner": "user"},
    ),
}


# ── Config helpers ──────────────────────────────────────────────────────────────

def redirect_uri() -> str:
    """The single server-side callback URL. Must match each provider's OAuth app."""
    return f"{settings.public_base_url.rstrip('/')}/connectors/oauth/callback"


def provider_configured(provider: str) -> bool:
    spec = PROVIDER_CATALOG.get(provider)
    if not spec:
        return False
    return bool(os.getenv(spec.client_id_env) and os.getenv(spec.client_secret_env))


def list_providers() -> list[dict[str, Any]]:
    """Catalog entries for the directory UI, each with a `configured` flag."""
    return [
        {
            "provider": spec.provider,
            "name": spec.name,
            "description": spec.description,
            "icon": spec.icon,
            "scopes": spec.scopes,
            "configured": provider_configured(spec.provider),
        }
        for spec in PROVIDER_CATALOG.values()
    ]


# ── Signed state ──────────────────────────────────────────────────────────────

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _state_secret() -> bytes:
    return settings.jwt_secret.encode()


def sign_state(payload: dict[str, Any]) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64e(hmac.new(_state_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_state(state: str, max_age_seconds: int | None = 600) -> dict[str, Any]:
    try:
        body, sig = state.split(".", 1)
    except ValueError as exc:
        raise ValueError("malformed state") from exc
    expected = _b64e(hmac.new(_state_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise ValueError("bad state signature")
    payload = json.loads(_b64d(body))
    if max_age_seconds is not None and "ts" in payload:
        if int(time.time()) - int(payload["ts"]) > max_age_seconds:
            raise ValueError("state expired")
    return payload


# ── Authorize URL ───────────────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = _b64e(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


async def build_authorize_url(provider: str, member_id: str, org_id: str) -> str:
    spec = PROVIDER_CATALOG.get(provider)
    if spec is None:
        raise ValueError(f"Unknown provider '{provider}'")
    if not provider_configured(provider):
        raise ValueError(f"Provider '{provider}' is not configured — set {spec.client_id_env}/{spec.client_secret_env}")

    state = sign_state({
        "member_id": member_id,
        "org_id": org_id,
        "provider": provider,
        "nonce": secrets.token_urlsafe(8),
        "ts": int(time.time()),
    })

    params: dict[str, str] = {
        "response_type": "code",
        "client_id": os.environ[spec.client_id_env],
        "redirect_uri": redirect_uri(),
        "scope": " ".join(spec.scopes),
        "state": state,
        **spec.extra_authorize_params,
    }
    if spec.pkce:
        verifier, challenge = _pkce_pair()
        await redis_client.set(f"oauth:pkce:{state}", verifier, ex=_PKCE_TTL_SECONDS)
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"

    return f"{spec.authorize_url}?{urlencode(params)}"


# ── Code exchange ───────────────────────────────────────────────────────────────

async def _post_token(url: str, data: dict[str, str]) -> dict[str, Any]:
    """POST the token request. Isolated so tests can mock the network boundary."""
    import httpx

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(url, data=data, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()


async def exchange_code(provider: str, code: str, state: str) -> dict[str, Any]:
    payload = verify_state(state)
    if payload.get("provider") != provider:
        raise ValueError("state/provider mismatch")
    spec = PROVIDER_CATALOG.get(provider)
    if spec is None:
        raise ValueError(f"Unknown provider '{provider}'")

    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
        "client_id": os.getenv(spec.client_id_env, ""),
        "client_secret": os.getenv(spec.client_secret_env, ""),
    }
    if spec.pkce:
        verifier = await redis_client.get(f"oauth:pkce:{state}")
        if verifier:
            data["code_verifier"] = verifier
            await redis_client.delete(f"oauth:pkce:{state}")

    tokens = await _post_token(spec.token_url, data)
    # Carry the member/org binding through (double-underscore keys are stripped
    # before anything is vaulted or returned).
    tokens["__member_id"] = payload.get("member_id")
    tokens["__org_id"] = payload.get("org_id", "default")
    return tokens


# ── Completing the connection ───────────────────────────────────────────────────

async def _insert_connector_row(values: dict[str, Any]) -> str:
    """Insert a connectors row, keeping only columns the table actually has."""
    connectors = await reflect_table("connectors")
    cols = set(connectors.c.keys())
    filtered = {k: v for k, v in values.items() if k in cols}
    async with engine.begin() as conn:
        result = await conn.execute(insert(connectors).values(**filtered).returning(connectors.c.id))
        return str(result.scalar_one())


async def complete_connection(provider: str, code: str, state: str) -> dict[str, Any]:
    """Exchange the code, vault the tokens, and activate a connectors row.

    Returns a non-secret summary (provider/status/account) — tokens never leave
    the vault boundary.
    """
    tokens = await exchange_code(provider, code, state)
    org_id = tokens.get("__org_id") or "default"
    account = tokens.get("__account")

    credentials = {k: v for k, v in tokens.items() if not k.startswith("__")}
    scope_value = credentials.get("scope") or ""
    scopes = scope_value.split() if isinstance(scope_value, str) else list(scope_value or [])

    connector_id = str(uuid.uuid4())
    vault_ref = await vault.store(connector_id, credentials, org_id=org_id)

    await _insert_connector_row({
        "id": connector_id,
        "organization_id": org_id,
        "region": settings.region,
        "provider": provider,
        "account_handle": account,
        "vault_ref": vault_ref,
        "status": "active",
        "scopes": scopes,
    })

    return {
        "provider": provider,
        "status": "active",
        "connector_id": connector_id,
        "account_handle": account,
        "scopes": scopes,
    }
