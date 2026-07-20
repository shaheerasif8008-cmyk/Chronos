"""Amazon Cognito ID token validation and OAuth code exchange."""

from __future__ import annotations

from functools import lru_cache
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from jwt import PyJWKClient

from core.config import settings
from core.ssrf import UnsafeURLError, assert_safe_url


class CognitoAuthError(Exception):
    """Raised when Cognito token validation or OAuth exchange fails."""


# Global client for connection pooling
_http_client = httpx.AsyncClient(timeout=30.0)


def cognito_enabled() -> bool:
    return settings.auth_provider in {"cognito", "both"} and bool(
        settings.cognito_user_pool_id
        and settings.cognito_app_client_id
        and settings.cognito_region
        and settings.cognito_domain
        and settings.cognito_callback_url
    )


def issuer() -> str:
    value = settings.cognito_issuer_url.strip() or (
        f"https://cognito-idp.{settings.cognito_region}.amazonaws.com/"
        f"{settings.cognito_user_pool_id}"
    )
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise CognitoAuthError("COGNITO_ISSUER_URL must be a valid HTTPS URL") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise CognitoAuthError("COGNITO_ISSUER_URL must be an absolute HTTPS URL")
    return value.rstrip("/")


def jwks_url() -> str:
    value = settings.cognito_jwks_url.strip() or f"{issuer()}/.well-known/jwks.json"
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise CognitoAuthError("COGNITO_JWKS_URL must be a valid HTTPS URL") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise CognitoAuthError("COGNITO_JWKS_URL must be an absolute HTTPS URL")
    try:
        return assert_safe_url(value)
    except UnsafeURLError as exc:
        raise CognitoAuthError("COGNITO_JWKS_URL must resolve to a public endpoint") from exc


def hosted_ui_base() -> str:
    if not settings.cognito_domain:
        raise CognitoAuthError("COGNITO_DOMAIN is not configured")
    domain = settings.cognito_domain
    if domain.startswith("http://") or domain.startswith("https://"):
        return domain.rstrip("/")
    return f"https://{domain}.auth.{settings.cognito_region}.amazoncognito.com"


@lru_cache
def _jwks_client() -> PyJWKClient:
    return PyJWKClient(jwks_url())


def build_authorize_url(*, state: str | None = None) -> str:
    redirect_uri = settings.cognito_callback_url
    params = {
        "client_id": settings.cognito_app_client_id,
        "response_type": "code",
        "scope": "openid email",
        "redirect_uri": redirect_uri,
    }
    if state:
        params["state"] = state
    return f"{hosted_ui_base()}/oauth2/authorize?{urlencode(params)}"


def verify_id_token(token: str) -> dict[str, Any]:
    """Validate a Cognito ID token and return its claims."""
    if not cognito_enabled():
        raise CognitoAuthError("Cognito auth is not enabled")
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.cognito_app_client_id,
            issuer=issuer(),
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise CognitoAuthError(f"Invalid Cognito token: {exc}") from exc

    token_use = claims.get("token_use")
    if token_use not in {None, "id"}:
        raise CognitoAuthError(f"Unexpected token_use: {token_use}")

    return claims


async def exchange_authorization_code(code: str, *, redirect_uri: str | None = None) -> dict[str, Any]:
    """Exchange an authorization code from the Cognito hosted UI for tokens."""
    if not cognito_enabled():
        raise CognitoAuthError("Cognito auth is not enabled")

    redirect = redirect_uri or settings.cognito_callback_url
    data = {
        "grant_type": "authorization_code",
        "client_id": settings.cognito_app_client_id,
        "code": code,
        "redirect_uri": redirect,
    }
    if settings.cognito_app_client_secret:
        data["client_secret"] = settings.cognito_app_client_secret

    response = await _http_client.post(
        f"{hosted_ui_base()}/oauth2/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    if response.status_code >= 400:
        # Surface the Cognito error code (e.g. "invalid_grant") for easier debugging.
        try:
            err_body = response.json()
            err_code = err_body.get("error", "unknown_error")
            err_desc = err_body.get("error_description", response.text)
        except Exception:
            err_code, err_desc = "token_exchange_error", response.text
        raise CognitoAuthError(f"Token exchange failed ({err_code}): {err_desc}")

    payload = response.json()
    id_token = payload.get("id_token")
    if not id_token:
        raise CognitoAuthError("Cognito did not return an id_token")

    claims = verify_id_token(id_token)
    return {"id_token": id_token, "access_token": payload.get("access_token"), "claims": claims}


def email_from_claims(claims: dict[str, Any]) -> str:
    email = (claims.get("email") or claims.get("username") or "").strip().lower()
    if not email or "@" not in email:
        raise CognitoAuthError("Cognito token is missing a valid email claim")
    return email
