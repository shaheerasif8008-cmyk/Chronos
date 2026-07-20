from __future__ import annotations
"""
Enterprise SSO — generic OIDC (OpenID Connect) against a per-org identity
provider (Okta, Microsoft Entra ID, Google Workspace, Auth0, Ping, OneLogin …).

A tenant configures an ``sso_connections`` row (issuer + client id/secret). Login
is routed to the right IdP by the user's email domain or an explicit org. The
Authorization Code flow runs here: build the authorize URL, exchange the code,
validate the id_token against the IdP's JWKS, and hand the verified claims back
to the router for JIT member provisioning.

State is a short-lived signed JWT (tenant + connection + redirect + nonce) and
is bound to the initiating browser with an HttpOnly cookie. Endpoint URLs can
be auto-discovered from the issuer's ``/.well-known/openid-configuration``
document.
"""
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
import jwt
from sqlalchemy import select

from core.config import settings
from core.db import engine, reflect_table
from core.ssrf import UnsafeURLError, assert_safe_url

_TIMEOUT = 15.0
_STATE_TTL_SECONDS = 600
_STATE_AUDIENCE = "chronos-sso-oauth"
_ENCRYPTED_SECRET_PREFIX = "enc:v1:"


class SSOError(Exception):
    """Raised when SSO configuration, exchange, or token validation fails."""


def _secret_cipher():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - a production dependency
        raise SSOError("SSO secret encryption is unavailable") from exc
    try:
        key = bytes.fromhex(settings.vault_encryption_key.strip())
    except ValueError as exc:
        raise SSOError("VAULT_ENCRYPTION_KEY is invalid") from exc
    if len(key) != 32:
        raise SSOError("VAULT_ENCRYPTION_KEY must be 32 bytes")
    return AESGCM(key)


def protect_client_secret(secret: str, *, organization_id: str) -> str:
    """Encrypt an OIDC client secret before persisting it.

    Local development without a vault key retains legacy plaintext behavior so
    the zero-config dev stack still works. Production already refuses to boot
    without a valid vault key, so every newly written production secret is
    AES-256-GCM encrypted and tenant-bound with associated data.
    """

    if not secret or secret.startswith(_ENCRYPTED_SECRET_PREFIX):
        return secret
    if not settings.vault_encryption_key.strip():
        if settings.is_production:
            raise SSOError("VAULT_ENCRYPTION_KEY is required for SSO secrets")
        return secret
    nonce = secrets.token_bytes(12)
    ciphertext = _secret_cipher().encrypt(
        nonce, secret.encode("utf-8"), organization_id.encode("utf-8")
    )
    return _ENCRYPTED_SECRET_PREFIX + urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def reveal_client_secret(value: str, *, organization_id: str) -> str:
    """Decrypt a tenant-bound OIDC client secret for the token exchange only."""

    if not value or not value.startswith(_ENCRYPTED_SECRET_PREFIX):
        return value
    try:
        raw = urlsafe_b64decode(value[len(_ENCRYPTED_SECRET_PREFIX):].encode("ascii"))
        plaintext = _secret_cipher().decrypt(
            raw[:12], raw[12:], organization_id.encode("utf-8")
        )
        return plaintext.decode("utf-8")
    except Exception as exc:
        raise SSOError("Stored SSO client secret could not be decrypted") from exc


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
    organization_id = str(row["organization_id"])
    return SSOConnection(
        id=str(row["id"]),
        organization_id=organization_id,
        issuer=str(row["issuer"]),
        client_id=str(row["client_id"]),
        client_secret=reveal_client_secret(
            str(row.get("client_secret") or ""), organization_id=organization_id
        ),
        authorize_url=str(row.get("authorize_url") or ""),
        token_url=str(row.get("token_url") or ""),
        jwks_url=str(row.get("jwks_url") or ""),
        userinfo_url=str(row.get("userinfo_url") or ""),
        scopes=str(row.get("scopes") or "openid email profile"),
        email_domain=(row.get("email_domain") or None),
        default_role=str(row.get("default_role") or "viewer"),
        enabled=bool(row.get("enabled", True)),
    )


# ── Discovery ────────────────────────────────────────────────────────────────

def _canonical_issuer(value: str) -> str:
    """Return a comparison-safe issuer, rejecting URL features OIDC never needs."""

    try:
        parsed = urlsplit(value.strip())
        parsed_port = parsed.port
    except ValueError as exc:
        raise SSOError("OIDC issuer must be a valid absolute HTTPS URL") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise SSOError("OIDC issuer must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SSOError("OIDC issuer must not contain credentials, a query, or a fragment")
    hostname = parsed.hostname.rstrip(".").lower()
    port = f":{parsed_port}" if parsed_port and parsed_port != 443 else ""
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", f"{hostname}{port}", path, "", ""))


def validate_oidc_url(value: str, *, label: str) -> str:
    """Validate an operator-supplied OIDC URL before any outbound request.

    OIDC credentials and tokens may only be sent to public HTTPS endpoints. The
    shared SSRF guard resolves every A/AAAA record and rejects loopback, private,
    link-local (including cloud metadata), reserved, and mixed public/private
    answers. Redirects are disabled by each caller so a validated endpoint cannot
    bounce the request to an unvalidated address.
    """

    value = value.strip()
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise SSOError(f"{label} must be a valid absolute HTTPS URL") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise SSOError(f"{label} must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise SSOError(f"{label} must not contain credentials or a fragment")
    try:
        return assert_safe_url(value)
    except UnsafeURLError as exc:
        raise SSOError(f"{label} is not a safe public endpoint") from exc


def validate_connection_shape(values: dict[str, Any]) -> None:
    """Cheap, DNS-free validation for connection create/update requests.

    DNS is deliberately checked immediately before each outbound request rather
    than only at configuration time, which also protects against records that
    change after an administrator saves the connection.
    """

    _canonical_issuer(str(values.get("issuer") or ""))
    for key, label in (
        ("authorize_url", "OIDC authorization endpoint"),
        ("token_url", "OIDC token endpoint"),
        ("jwks_url", "OIDC JWKS endpoint"),
        ("userinfo_url", "OIDC userinfo endpoint"),
    ):
        value = str(values.get(key) or "").strip()
        if not value:
            continue
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError as exc:
            raise SSOError(f"{label} must be a valid absolute HTTPS URL") from exc
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise SSOError(f"{label} must be an absolute HTTPS URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise SSOError(f"{label} must not contain credentials or a fragment")


async def discover(issuer: str) -> dict[str, str]:
    """Fetch and validate an IdP's OIDC discovery document.

    The discovery issuer must exactly match the configured issuer after harmless
    trailing-slash/case normalization. Every returned endpoint is independently
    SSRF-checked and redirects are refused. The resulting endpoint set is the
    per-connection allowlist used by the authorization-code flow.
    """

    canonical = _canonical_issuer(issuer)
    url = validate_oidc_url(
        canonical.rstrip("/") + "/.well-known/openid-configuration",
        label="OIDC discovery endpoint",
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise SSOError("OIDC discovery request failed") from exc
    if resp.is_redirect:
        raise SSOError("OIDC discovery endpoint redirects are not allowed")
    if resp.status_code >= 400:
        raise SSOError(f"OIDC discovery failed: HTTP {resp.status_code}")
    try:
        doc = resp.json()
    except ValueError as exc:
        raise SSOError("OIDC discovery returned invalid JSON") from exc
    if not isinstance(doc, dict) or _canonical_issuer(str(doc.get("issuer") or "")) != canonical:
        raise SSOError("OIDC discovery issuer did not match the configured issuer")
    endpoints = {
        "authorize_url": str(doc.get("authorization_endpoint") or ""),
        "token_url": str(doc.get("token_endpoint") or ""),
        "jwks_url": str(doc.get("jwks_uri") or ""),
        "userinfo_url": str(doc.get("userinfo_endpoint") or ""),
    }
    for key, label in (
        ("authorize_url", "OIDC authorization endpoint"),
        ("token_url", "OIDC token endpoint"),
        ("jwks_url", "OIDC JWKS endpoint"),
    ):
        if not endpoints[key]:
            raise SSOError(f"OIDC discovery omitted the {label.lower()}")
        validate_oidc_url(endpoints[key], label=label)
    if endpoints["userinfo_url"]:
        validate_oidc_url(endpoints["userinfo_url"], label="OIDC userinfo endpoint")
    return endpoints


async def _resolved_endpoints(conn: SSOConnection) -> SSOConnection:
    """Ensure authorize/token/jwks URLs are populated (discover lazily if blank)."""
    _canonical_issuer(conn.issuer)
    validate_oidc_url(conn.issuer, label="OIDC issuer")
    found: dict[str, str] = {}
    if not (conn.authorize_url and conn.token_url and conn.jwks_url):
        found = await discover(conn.issuer)
    resolved = SSOConnection(
        **{
            **conn.__dict__,
            "authorize_url": conn.authorize_url or found.get("authorize_url", ""),
            "token_url": conn.token_url or found.get("token_url", ""),
            "jwks_url": conn.jwks_url or found.get("jwks_url", ""),
            "userinfo_url": conn.userinfo_url or found.get("userinfo_url", ""),
        }
    )
    issuer_host = (urlsplit(conn.issuer).hostname or "").rstrip(".").lower()
    allowed_hosts = {
        host.strip().rstrip(".").lower()
        for host in settings.sso_endpoint_host_allowlist.split(",")
        if host.strip()
    }
    allowed_hosts.add(issuer_host)
    # A validated discovery document is the per-connection endpoint allowlist.
    allowed_hosts.update(
        (urlsplit(value).hostname or "").rstrip(".").lower()
        for value in found.values()
        if value
    )
    for value, label in (
        (resolved.authorize_url, "OIDC authorization endpoint"),
        (resolved.token_url, "OIDC token endpoint"),
        (resolved.jwks_url, "OIDC JWKS endpoint"),
    ):
        if not value:
            raise SSOError(f"{label} is not configured")
        validate_oidc_url(value, label=label)
        endpoint_host = (urlsplit(value).hostname or "").rstrip(".").lower()
        if endpoint_host not in allowed_hosts:
            raise SSOError(
                f"{label} host is not allowlisted for this OIDC issuer"
            )
    if resolved.userinfo_url:
        validate_oidc_url(resolved.userinfo_url, label="OIDC userinfo endpoint")
        userinfo_host = (urlsplit(resolved.userinfo_url).hostname or "").rstrip(".").lower()
        if userinfo_host not in allowed_hosts:
            raise SSOError("OIDC userinfo endpoint host is not allowlisted for this OIDC issuer")
    return resolved


# ── Connection lookup ────────────────────────────────────────────────────────

async def get_connection_by_id(
    connection_id: str, *, organization_id: str | None = None
) -> SSOConnection | None:
    table = await reflect_table("sso_connections")
    clauses = [table.c.id == connection_id]
    if organization_id is not None:
        clauses.append(table.c.organization_id == organization_id)
    async with engine.begin() as conn:
        row = (await conn.execute(select(table).where(*clauses))).mappings().first()
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

def sign_state(connection_id: str, organization_id: str, redirect: str, nonce: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "aud": _STATE_AUDIENCE,
            "purpose": "sso_login",
            "cid": connection_id,
            "org": organization_id,
            "redirect": redirect,
            "nonce": nonce,
            "jti": secrets.token_urlsafe(18),
            "iat": now,
            "exp": now + _STATE_TTL_SECONDS,
        },
        settings.jwt_secret, algorithm="HS256",
    )


def verify_state(state: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            state,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=_STATE_AUDIENCE,
            options={
                "require": [
                    "exp", "iat", "aud", "purpose", "cid", "org", "nonce", "jti"
                ]
            },
        )
    except jwt.PyJWTError as exc:
        raise SSOError("Invalid or expired SSO state") from exc
    if payload.get("purpose") != "sso_login":
        raise SSOError("Invalid or expired SSO state")
    return payload


# ── Authorization Code flow ──────────────────────────────────────────────────

def _redirect_uri() -> str:
    # The IdP redirects back to the API callback, which then bounces to the web app.
    return settings.oauth_callback_base_url.rstrip("/") + "/auth/sso/callback"


async def build_login_url(
    conn: SSOConnection, *, redirect: str, nonce: str, state: str | None = None
) -> str:
    conn = await _resolved_endpoints(conn)
    state = state or sign_state(conn.id, conn.organization_id, redirect, nonce)
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


async def _fetch_jwks(jwks_url: str) -> dict[str, Any]:
    url = validate_oidc_url(jwks_url, label="OIDC JWKS endpoint")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise SSOError("OIDC JWKS request failed") from exc
    if resp.is_redirect:
        raise SSOError("OIDC JWKS endpoint redirects are not allowed")
    if resp.status_code >= 400:
        raise SSOError(f"OIDC JWKS request failed: HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SSOError("OIDC JWKS endpoint returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
        raise SSOError("OIDC JWKS endpoint returned an invalid key set")
    return payload


def _signing_key_from_jwks(id_token: str, jwks: dict[str, Any]) -> Any:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise SSOError("Invalid id_token header") from exc
    algorithm = str(header.get("alg") or "")
    if algorithm not in {"RS256", "ES256"}:
        raise SSOError("id_token uses an unsupported signing algorithm")
    kid = str(header.get("kid") or "")
    if not kid:
        raise SSOError("id_token is missing a signing-key id")
    for candidate in jwks.get("keys", []):
        if isinstance(candidate, dict) and secrets.compare_digest(str(candidate.get("kid") or ""), kid):
            try:
                key = jwt.PyJWK.from_dict(candidate, algorithm=algorithm)
            except (jwt.PyJWTError, ValueError) as exc:
                raise SSOError("OIDC JWKS contained an invalid signing key") from exc
            return key.key
    raise SSOError("No OIDC signing key matched the id_token")


async def exchange_code(
    conn: SSOConnection, code: str, *, expected_nonce: str
) -> dict[str, Any]:
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
    validate_oidc_url(conn.token_url, label="OIDC token endpoint")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await client.post(
                conn.token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as exc:
        raise SSOError("Token exchange request failed") from exc
    if resp.is_redirect:
        raise SSOError("OIDC token endpoint redirects are not allowed")
    if resp.status_code >= 400:
        raise SSOError(f"Token exchange failed: HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SSOError("Token exchange returned invalid JSON") from exc
    id_token = payload.get("id_token")
    if not id_token:
        raise SSOError("IdP did not return an id_token")
    jwks = await _fetch_jwks(conn.jwks_url)
    return verify_id_token(conn, id_token, jwks=jwks, expected_nonce=expected_nonce)


def verify_id_token(
    conn: SSOConnection,
    id_token: str,
    *,
    jwks: dict[str, Any],
    expected_nonce: str,
) -> dict[str, Any]:
    try:
        signing_key = _signing_key_from_jwks(id_token, jwks)
        claims = jwt.decode(
            id_token, signing_key, algorithms=["RS256", "ES256"],
            audience=conn.client_id, issuer=_canonical_issuer(conn.issuer),
            options={"require": ["exp", "iat", "sub", "nonce"]},
        )
    except (jwt.PyJWTError, SSOError) as exc:
        if isinstance(exc, SSOError):
            raise
        raise SSOError("Invalid id_token") from exc
    token_nonce = str(claims.get("nonce") or "")
    if not token_nonce or not secrets.compare_digest(token_nonce, expected_nonce):
        raise SSOError("id_token nonce did not match this SSO login")
    return claims


def email_from_claims(claims: dict[str, Any]) -> str:
    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if not email or "@" not in email:
        raise SSOError("id_token is missing a valid email claim")
    return email
