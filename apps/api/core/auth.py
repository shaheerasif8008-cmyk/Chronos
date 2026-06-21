from __future__ import annotations
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Cookie, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from core.config import settings
from core.db import engine, reflect_table
from core.models import Member

bearer = HTTPBearer(auto_error=False)


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
    response.set_cookie(
        "chronos_session",
        token,
        httponly=True,
        samesite="none" if settings.is_production else "lax",
        secure=settings.is_production,
        max_age=settings.access_token_expire_minutes * 60,
    )


# W1 Phase 2 flip: these mint sites must pass `org_id=member.organization_id` so
# tokens become org-bound, and grandfathering of org-less tokens must then be
# closed (reject tokens with no `org` claim once enforcement is on):
#   routers/auth.py (OTP verify, Cognito callback, Cognito verify) and routers/sso.py.
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
    # Tenant binding: an org-bound token is valid only on its own tenant. Legacy
    # tokens (no `org` claim) are grandfathered. Fail closed when the resolved
    # tenant is known and does not match.
    # NOTE (W1 Phase 2): when `resolved` is None (apex / unknown subdomain / a
    # resolver failure), an org-bound token is currently ACCEPTED. Harmless in
    # Phase 1 (no path mints org-bound tokens yet). Before Phase 2 flips minting,
    # decide the no-tenant-host policy explicitly (reject vs. handoff-endpoint
    # exception) and pin it with a test. See docs/superpowers/plans Phase 2.
    token_org = payload.get("org")
    if token_org is not None:
        resolved = getattr(request.state, "resolved_org_id", None)
        if resolved is not None and resolved != token_org:
            raise HTTPException(status_code=403, detail="Token not valid for this tenant")
    return member
