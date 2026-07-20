from __future__ import annotations
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import jwt
from fastapi import Cookie, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from core.config import settings
from core.db import engine, reflect_table
from core.models import Member

log = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)
_COGNITO_STATE_AUDIENCE = "chronos-cognito-oauth"
_COGNITO_STATE_TTL_SECONDS = 10 * 60


def _org_bound_grace_active() -> bool:
    """True while the org-binding migration grace window is open.

    During the window, legacy org-less tokens are still accepted in production so
    active sessions are not abruptly invalidated when enforcement is first turned
    on. Empty/unset or unparseable ``org_bound_tokens_grace_until`` → no grace."""
    raw = (settings.org_bound_tokens_grace_until or "").strip()
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < until


def _is_production() -> bool:
    # Thin shim over settings.is_production. Exists solely so tests can monkeypatch
    # the C1 production-reject branch without patching the whole settings object.
    # NOT the canonical production flag — other code reads settings.is_production directly.
    return settings.is_production


def _is_configured_central_api_host(request: Request) -> bool:
    """Return whether the request targets the explicitly configured API origin.

    Production uses one central API hostname while tenant identity lives in the
    signed session claim and the member row. The central hostname is therefore
    a valid authenticated entrypoint, but an arbitrary unknown/apex Host is not.
    Comparing against the configured OAuth callback origin avoids trusting a
    caller-controlled organization header or accepting every reserved hostname.
    """

    configured_host = urlsplit(settings.oauth_callback_base_url).hostname
    request_host = request.url.hostname
    if not configured_host or not request_host:
        return False
    return request_host.rstrip(".").lower() == configured_host.rstrip(".").lower()


def create_cognito_oauth_state(*, org_id: str, subdomain: str) -> str:
    """Create a signed, short-lived tenant binding for the Cognito round trip."""

    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "aud": _COGNITO_STATE_AUDIENCE,
            "purpose": "cognito_login",
            "org": str(org_id),
            "tenant": subdomain.strip().lower(),
            "nonce": secrets.token_urlsafe(24),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=_COGNITO_STATE_TTL_SECONDS)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_cognito_oauth_state(token: str) -> dict[str, str]:
    """Verify a Cognito state token and return its tenant claims."""

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=_COGNITO_STATE_AUDIENCE,
            options={"require": ["exp", "iat", "aud", "purpose", "org", "tenant", "nonce"]},
        )
    except jwt.PyJWTError as exc:
        raise ValueError("Invalid or expired Cognito login state") from exc
    if payload.get("purpose") != "cognito_login":
        raise ValueError("Invalid or expired Cognito login state")
    return {
        "org": str(payload["org"]),
        "tenant": str(payload["tenant"]),
        "nonce": str(payload["nonce"]),
    }


def set_session_cookie(response, token: str) -> None:
    """Set the session JWT as an httpOnly cookie.

    The web app and API are served from different hosts (``app.<domain>`` vs
    ``api.<domain>``), so requests are cross-origin but still same-site. A
    host-only ``SameSite=Lax`` cookie is therefore sent to the API during the
    SPA's credentialed fetches without exposing the session to every tenant
    subdomain or enabling ordinary cross-site request forgery.
    """
    response.set_cookie(
        "chronos_session",
        token,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        max_age=settings.access_token_expire_minutes * 60,
    )


def clear_session_cookie(response) -> None:
    """Expire the session cookie using the same scope used when it was set."""

    response.delete_cookie(
        "chronos_session",
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
    )


def create_access_token(member_id: str, *, org_id: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": member_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_expire_minutes)).timestamp()),
    }
    if org_id is not None:
        payload["org"] = org_id
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


async def get_current_member(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    chronos_session: str | None = Cookie(default=None),
) -> Member:
    token = credentials.credentials if credentials is not None else chronos_session
    if not token:
        raise HTTPException(status_code=401, detail="Missing session token")
    if credentials is not None and token.startswith("chr_live_"):
        from core.organization_api_keys import authenticate_api_key

        member = await authenticate_api_key(token, request)
        resolved = getattr(request.state, "resolved_org_id", None)
        if resolved is not None and str(resolved) != str(member.organization_id):
            raise HTTPException(status_code=403, detail="API key not valid for this tenant")
        if resolved is None and _is_production() and not _is_configured_central_api_host(request):
            raise HTTPException(status_code=403, detail="API key not valid for this tenant")
        return member
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc

    subject = payload.get("sub")
    if not subject:
        raise HTTPException(status_code=401, detail="Invalid bearer token")

    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(select(members).where(members.c.id == subject))
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=401, detail="Member not found")
    member = Member(**dict(row))
    # SCIM deprovisioning (or an admin) can deactivate a member; a deactivated
    # member's existing tokens must stop working immediately.
    if getattr(member, "status", "active") != "active":
        raise HTTPException(status_code=403, detail="Member account is deactivated")
    # Org-bound tokens: a session token must carry its tenant (`org`) claim.
    # Enforced in production by default; dev/test keep org-less tokens working
    # (every minted token already carries `org`, so an org-less token in prod is
    # a stale legacy session). During the migration grace window, legacy tokens
    # are accepted with a warning so active ≤1h sessions drain rather than break.
    if (
        payload.get("org") is None
        and settings.enforce_org_bound_tokens
        and _is_production()
    ):
        if _org_bound_grace_active():
            log.warning(
                "Accepting legacy org-less session token for member %s during the "
                "org-binding grace window",
                subject,
            )
        else:
            raise HTTPException(status_code=401, detail="Session token missing tenant binding")

    # Tenant binding (secondary defense — data isolation is enforced downstream by
    # member.organization_id scoping, not by this check). An org-bound token is
    # valid only on its own tenant.
    token_org = payload.get("org")
    if token_org is not None:
        # A valid signature alone is not enough: bind the claim to the current
        # database membership so a stale or incorrectly minted token cannot
        # cross organizations even on the central API host.
        if str(member.organization_id) != str(token_org):
            raise HTTPException(status_code=403, detail="Token not valid for this tenant")
        resolved = getattr(request.state, "resolved_org_id", None)
        if resolved is None:
            # The production SPA talks to one configured central API host. On
            # that host the signed org claim, cross-checked against the member
            # row above, is the tenant context. Unknown/apex hosts still fail
            # closed; non-production keeps localhost ergonomics.
            if _is_production() and not _is_configured_central_api_host(request):
                raise HTTPException(status_code=403, detail="Token not valid for this tenant")
        elif resolved != token_org:
            raise HTTPException(status_code=403, detail="Token not valid for this tenant")
    return member
