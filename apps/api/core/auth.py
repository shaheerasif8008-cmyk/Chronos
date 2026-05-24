"""Authentication helpers.

Two token paths co-exist:

1. **Chronos-issued HS256 JWT** — created after a successful Cognito code exchange
   (or the legacy OTP flow in dev).  Validated by :func:`get_current_member`.
2. **Cognito RS256 ID token** — validated once during the callback, then exchanged
   for a Chronos token.  Validated by :func:`verify_cognito_token`.

``get_current_member`` only needs to verify path-1 tokens; path-2 is handled
entirely inside the callback endpoint and never reaches regular API routes.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from core.config import settings
from core.db import engine, reflect_table
from core.models import Member

bearer = HTTPBearer(auto_error=False)


# ── Chronos-issued tokens (HS256) ──────────────────────────────────────────────

def create_access_token(member_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": member_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


async def get_current_member(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Member:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc

    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(select(members).where(members.c.id == payload["sub"]))
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=401, detail="Member not found")
    return Member(**dict(row))


# ── Cognito RS256 ID-token verification ────────────────────────────────────────
#
# PyJWT's PyJWKClient handles JWKS fetching, key rotation, and caching
# (5-minute lifespan by default).  It makes a blocking HTTP call, so we
# dispatch it to a thread pool via asyncio.to_thread.

_jwks_client: jwt.PyJWKClient | None = None


def _get_jwks_client() -> jwt.PyJWKClient:
    """Lazily build the JWKS client so settings are available at call time."""
    global _jwks_client
    if _jwks_client is None:
        jwks_url = (
            f"https://cognito-idp.{settings.cognito_region}.amazonaws.com"
            f"/{settings.cognito_user_pool_id}/.well-known/jwks.json"
        )
        _jwks_client = jwt.PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300)
    return _jwks_client


def _verify_cognito_token_sync(id_token: str) -> dict[str, Any]:
    """Synchronous inner — run via asyncio.to_thread."""
    client = _get_jwks_client()
    signing_key = client.get_signing_key_from_jwt(id_token)

    issuer = (
        f"https://cognito-idp.{settings.cognito_region}.amazonaws.com"
        f"/{settings.cognito_user_pool_id}"
    )
    claims: dict[str, Any] = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=settings.cognito_app_client_id,
        issuer=issuer,
        options={"verify_exp": True},
    )

    if claims.get("token_use") != "id":
        raise ValueError("Expected an ID token (token_use='id'), got something else")

    return claims


async def verify_cognito_token(id_token: str) -> dict[str, Any]:
    """Verify a Cognito ID token (RS256) and return its decoded claims.

    Raises :class:`ValueError` on any verification failure so callers can
    surface a 401.
    """
    if not settings.cognito_user_pool_id:
        raise ValueError("Cognito is not configured (COGNITO_USER_POOL_ID is empty)")

    try:
        return await asyncio.to_thread(_verify_cognito_token_sync, id_token)
    except jwt.PyJWTError as exc:
        raise ValueError(f"Token verification failed: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"Token verification error: {exc}") from exc
