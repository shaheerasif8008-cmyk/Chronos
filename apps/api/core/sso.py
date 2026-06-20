from __future__ import annotations
"""
Enterprise SSO — generic OIDC (OpenID Connect) against a per-org identity
provider (Okta, Microsoft Entra ID, Google Workspace, Auth0, Ping, OneLogin …).

A tenant configures an ``sso_connections`` row (issuer + client id/secret). Login
is routed to the right IdP by the user's email domain or an explicit org. The
Authorization Code flow runs here: build the authorize URL, exchange the code,
validate the id_token against the IdP's JWKS, and hand the verified claims back
to the router for JIT member provisioning.

State is a short-lived signed JWT (connection id + redirect + nonce) so no
server-side session store is needed. Endpoint URLs can be auto-discovered from
the issuer's ``/.well-known/openid-configuration`` document.
"""
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient
from sqlalchemy import select

from core.config import settings
from core.db import engine, reflect_table

_TIMEOUT = 15.0
_STATE_TTL_SECONDS = 600


class SSOError(Exception):
    """Raised when SSO configuration, exchange, or token validation fails."""


@dataclass(frozen=True)
class SSOConnection:
    id: str
    organization_id: str
    issuer: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    jwks_url: str
    userinfo_url: str
    scopes: str
    email_domain: str | None
    default_role: str
    enabled: bool


def _row_to_connection(row: dict) -> SSOConnection:
    return SSOConnection(
        id=str(row["id"]),
        organization_id=str(row["organization_id"]),
        issuer=str(row["issuer"]),
        client_id=str(row["client_id"]),
        client_secret=str(row.get("client_secret") or ""),
        authorize_url=str(row.get("authorize_url") or ""),
        token_url=str(row.get("token_url") or ""),
        jwks_url=str(row.get("jwks_url") or ""),
        userinfo_url=str(row.get("userinfo_url") or ""),
        scopes=str(row.get("scopes") or "openid email profile"),
        email_domain=(row.get("email_domain") or None),
        default_role=str(row.get("default_role") or "user"),
        enabled=bool(row.get("enabled", True)),
    )


# ── Discovery ────────────────────────────────────────────────────────────────

async def discover(issuer: str) -> dict[str, str]:
    """Fetch the IdP's OIDC discovery document and return endpoint URLs."""
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url)
    if resp.status_code >= 400:
        raise SSOError(f"OIDC discovery failed for {issuer}: {resp.status_code}")
    doc = resp.json()
    return {
        "authorize_url": doc.get("authorization_endpoint", ""),
        "token_url": doc.get("token_endpoint", ""),
        "jwks_url": doc.get("jwks_uri", ""),
        "userinfo_url": doc.get("userinfo_endpoint", ""),
    }


async def _resolved_endpoints(conn: SSOConnection) -> SSOConnection:
    """Ensure authorize/token/jwks URLs are populated (discover lazily if blank)."""
    if conn.authorize_url and conn.token_url and conn.jwks_url:
        return conn
    found = await discover(conn.issuer)
    return SSOConnection(
        **{**conn.__dict__,
           "authorize_url": conn.authorize_url or found["authorize_url"],
           "token_url": conn.token_url or found["token_url"],
           "jwks_url": conn.jwks_url or found["jwks_url"],
           "userinfo_url": conn.userinfo_url or found["userinfo_url"]}
    )


# ── Connection lookup ────────────────────────────────────────────────────────

async def get_connection_by_id(connection_id: str) -> SSOConnection | None:
    table = await reflect_table("sso_connections")
    async with engine.begin() as conn:
        row = (await conn.execute(select(table).where(table.c.id == connection_id))).mappings().first()
    return _row_to_connection(dict(row)) if row else None


async def get_connection_by_domain(email: str) -> SSOConnection | None:
    domain = email.split("@", 1)[-1].lower().strip()
    if not domain:
        return None
    table = await reflect_table("sso_connections")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table).where(table.c.email_domain == domain, table.c.enabled.is_(True))
            )
        ).mappings().first()
    return _row_to_connection(dict(row)) if row else None


async def get_connection_for_org(org_id: str) -> SSOConnection | None:
    table = await reflect_table("sso_connections")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table).where(table.c.organization_id == org_id, table.c.enabled.is_(True))
                .order_by(table.c.created_at.asc())
            )
        ).mappings().first()
    return _row_to_connection(dict(row)) if row else None


# ── State signing ────────────────────────────────────────────────────────────

def sign_state(connection_id: str, redirect: str, nonce: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"cid": connection_id, "redirect": redirect, "nonce": nonce,
         "iat": now, "exp": now + _STATE_TTL_SECONDS},
        settings.jwt_secret, algorithm="HS256",
    )


def verify_state(state: str) -> dict[str, Any]:
    try:
        return jwt.decode(state, settings.jwt_secret, algorithms=["HS256"],
                          options={"require": ["exp", "cid"]})
    except jwt.PyJWTError as exc:
        raise SSOError(f"Invalid or expired SSO state: {exc}") from exc


# ── Authorization Code flow ──────────────────────────────────────────────────

def _redirect_uri() -> str:
    # The IdP redirects back to the API callback, which then bounces to the web app.
    return settings.oauth_callback_base_url.rstrip("/") + "/auth/sso/callback"


async def build_login_url(conn: SSOConnection, *, redirect: str, nonce: str) -> str:
    conn = await _resolved_endpoints(conn)
    state = sign_state(conn.id, redirect, nonce)
    params = {
        "client_id": conn.client_id,
        "response_type": "code",
        "scope": conn.scopes,
        "redirect_uri": _redirect_uri(),
        "state": state,
        "nonce": nonce,
    }
    sep = "&" if "?" in conn.authorize_url else "?"
    return f"{conn.authorize_url}{sep}{urlencode(params)}"


def _jwks_client(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(jwks_url)


async def exchange_code(conn: SSOConnection, code: str) -> dict[str, Any]:
    """Exchange an authorization code for tokens and return verified id_token claims."""
    conn = await _resolved_endpoints(conn)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(),
        "client_id": conn.client_id,
    }
    if conn.client_secret:
        data["client_secret"] = conn.client_secret
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(conn.token_url, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    if resp.status_code >= 400:
        raise SSOError(f"Token exchange failed: {resp.status_code} {resp.text[:200]}")
    payload = resp.json()
    id_token = payload.get("id_token")
    if not id_token:
        raise SSOError("IdP did not return an id_token")
    return verify_id_token(conn, id_token)


def verify_id_token(conn: SSOConnection, id_token: str) -> dict[str, Any]:
    try:
        signing_key = _jwks_client(conn.jwks_url).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token, signing_key.key, algorithms=["RS256", "ES256"],
            audience=conn.client_id, issuer=conn.issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise SSOError(f"Invalid id_token: {exc}") from exc
    return claims


def email_from_claims(claims: dict[str, Any]) -> str:
    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if not email or "@" not in email:
        raise SSOError("id_token is missing a valid email claim")
    return email
