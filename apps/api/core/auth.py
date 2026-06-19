from __future__ import annotations
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from core.config import settings
from core.db import engine, reflect_table
from core.models import Member

bearer = HTTPBearer(auto_error=False)


def set_session_cookie(response, token: str) -> None:
    """Set the session JWT as an httpOnly cookie.

    ``secure`` is enabled outside development so the session token is never sent
    over plaintext HTTP; ``samesite=strict`` mitigates CSRF on the cookie path.
    """
    response.set_cookie(
        "chronos_session",
        token,
        httponly=True,
        samesite="strict" if settings.is_production else "lax",
        secure=settings.is_production,
        max_age=settings.access_token_expire_minutes * 60,
    )


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
        payload = jwt.decode(
            credentials.credentials,
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
    return Member(**dict(row))
