from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Cookie, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from core.config import settings
from core.db import engine, reflect_table
from core.models import Member

log = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


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


def set_session_cookie(response, token: str) -> None:
    """Set the session JWT as an httpOnly cookie.

    The web app and API are served from different hosts (``app.<domain>`` vs
    ``api.<domain>``), so every authenticated request the SPA makes is a
    cross-origin credentialed fetch, and the Cognito hosted-UI sign-in returns
    through a cross-site redirect. A ``SameSite=Strict``/``Lax`` cookie is not
    sent on (and, across registrable domains, not even stored from) those
    cross-site requests, which silently breaks the session right after login.
    In production we therefore use ``SameSite=None`` — which requires
    ``Secure`` — so the cookie travels with the SPA's credentialed calls. In
    development (plaintext HTTP, same-host) ``Lax`` is kept since ``None``
    without ``Secure`` is rejected by browsers.
    """
    # In production, scope the cookie to the parent domain (.<base_domain>) so a
    # cookie set during apex signup is valid on the tenant subdomain after redirect
    # (W1 Phase 2C). Host-only in dev.
    domain = f".{settings.base_domain}" if _is_production() else None
    response.set_cookie(
        "chronos_session",
        token,
        domain=domain,
        httponly=True,
        samesite="none" if settings.is_production else "lax",
        secure=settings.is_production,
        max_age=settings.access_token_expire_minutes * 60,
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
        resolved = getattr(request.state, "resolved_org_id", None)
        if resolved is None:
            # No tenant resolved from the host. In production this is the apex /
            # an unknown subdomain — reject (C1): the app only serves authed
            # traffic on a tenant subdomain. In non-production there is no
            # wildcard DNS (Host is "test"/localhost), so trust the token's org.
            if _is_production():
                raise HTTPException(status_code=403, detail="Token not valid for this tenant")
        elif resolved != token_org:
            raise HTTPException(status_code=403, detail="Token not valid for this tenant")
    return member
