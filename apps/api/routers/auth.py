"""Auth router.

Provides two authentication paths:

1. **Cognito Hosted UI** (production): ``GET /auth/cognito/authorize`` returns the
   sign-in URL; ``POST /auth/cognito/callback`` exchanges the authorization code for
   tokens, verifies the ID token, and issues a Chronos session JWT.

2. **OTP (dev fallback)**: ``POST /auth/request-otp`` / ``POST /auth/verify-otp``
   print the code to the console. Enabled only when ``settings.cognito_domain`` is
   not set, so there's no accidental bypass in production.
"""
from __future__ import annotations

import hashlib
import hmac
import random
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import insert, select, update

from core import audit
from core.auth import create_access_token, verify_cognito_token
from core.config import settings
from core.db import engine, reflect_table

router = APIRouter(prefix="/auth", tags=["auth"])

# Dev-only in-memory OTP store.  Never used when Cognito is configured.
_otp_store: dict[str, str] = {}


# ── OAuth state (CSRF protection) ─────────────────────────────────────────────
#
# State is a signed nonce: `<nonce>.<HMAC-SHA256(nonce, jwt_secret)>`.
# No server-side storage is required — the signature is self-verifying.
# The frontend stores the raw state string in sessionStorage between the
# authorize redirect and the callback page, then forwards it to the backend.

def _build_state() -> str:
    """Return a cryptographically random, HMAC-signed state value."""
    nonce = secrets.token_urlsafe(32)
    sig = hmac.new(settings.jwt_secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return f"{nonce}.{sig}"


def _verify_state(state: str) -> bool:
    """Return True iff state was signed by us and hasn't been tampered with."""
    try:
        nonce, sig = state.rsplit(".", 1)
    except ValueError:
        return False
    expected = hmac.new(settings.jwt_secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


# ── Cognito Hosted UI ──────────────────────────────────────────────────────────

@router.get("/cognito/authorize")
async def cognito_authorize() -> dict[str, str]:
    """Return the Cognito Hosted UI sign-in URL with a CSRF-protection state.

    The frontend stores the returned ``state`` in ``sessionStorage``, then
    redirects the browser to ``authorize_url``.  Cognito echoes the state back
    in the callback; the frontend forwards it to ``POST /cognito/callback`` for
    server-side HMAC verification before the code is exchanged.
    """
    if not settings.cognito_domain:
        raise HTTPException(status_code=503, detail="Cognito is not configured")

    state = _build_state()
    params = urlencode({
        "client_id": settings.cognito_app_client_id,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": settings.cognito_callback_url,
        "state": state,
    })
    return {
        "authorize_url": f"{settings.cognito_domain}/oauth2/authorize?{params}",
        "state": state,
    }


class CognitoCallbackRequest(BaseModel):
    code: str
    state: str  # CSRF guard — must match the value from /cognito/authorize


@router.post("/cognito/callback")
async def cognito_callback(req: CognitoCallbackRequest, response: Response) -> dict[str, str]:
    """Exchange the Cognito authorization code for a Chronos session.

    Steps:
    1. Verify the OAuth state to prevent CSRF.
    2. POST the code to Cognito's token endpoint.
    3. Verify the returned ID token (RS256, JWKS).
    4. Find or auto-create the member row (first login creates the account).
    5. Issue a Chronos HS256 JWT and set an httpOnly session cookie.
    """
    if not settings.cognito_domain:
        raise HTTPException(status_code=503, detail="Cognito is not configured")

    # ── 1. CSRF — verify state ─────────────────────────────────────────────────
    if not _verify_state(req.state):
        raise HTTPException(status_code=400, detail="Invalid or tampered OAuth state — possible CSRF")

    # ── 2. Exchange code for tokens ────────────────────────────────────────────
    token_url = f"{settings.cognito_domain}/oauth2/token"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                token_url,
                data={
                    "grant_type": "authorization_code",
                    "client_id": settings.cognito_app_client_id,
                    "client_secret": settings.cognito_app_client_secret,
                    "code": req.code,
                    "redirect_uri": settings.cognito_callback_url,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            tokens = resp.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Cognito token exchange failed: {exc}") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Cognito: {exc}") from exc

    # ── 3. Verify ID token ─────────────────────────────────────────────────────
    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(status_code=502, detail="Cognito did not return an ID token")

    try:
        claims = await verify_cognito_token(id_token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    email: str = claims.get("email", "")
    if not email:
        raise HTTPException(status_code=401, detail="Cognito ID token missing email claim")
    email = email.lower()

    # ── 4. Find or create member ───────────────────────────────────────────────
    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(select(members).where(members.c.email == email))
        ).mappings().first()

        if row is None:
            # Auto-provision on first Cognito sign-in.
            name: str = (
                claims.get("name")
                or f"{claims.get('given_name', '')} {claims.get('family_name', '')}".strip()
                or email.split("@")[0]
            )
            result = await conn.execute(
                insert(members)
                .values(
                    organization_id=settings.org_id,
                    email=email,
                    role="user",
                    name=name,
                    region=settings.region,
                )
                .returning(*members.c)
            )
            row = result.mappings().first()

    member_id = str(row["id"])

    # ── 5. Issue Chronos session ───────────────────────────────────────────────
    chronos_token = create_access_token(member_id)
    await audit.log("cognito_login", member_id, "auth.cognito_callback")
    response.set_cookie("chronos_session", chronos_token, httponly=True, samesite="lax")
    return {
        "access_token": chronos_token,
        "token_type": "bearer",
        "member_id": member_id,
    }


# ── OTP (dev fallback — disabled when Cognito is configured) ──────────────────

class OtpRequest(BaseModel):
    email: EmailStr


class OtpVerify(BaseModel):
    email: EmailStr
    code: str


@router.post("/request-otp")
async def request_otp(req: OtpRequest) -> dict[str, str]:
    if settings.cognito_domain:
        raise HTTPException(status_code=404, detail="OTP auth is disabled — use Cognito")
    code = f"{random.randint(0, 999999):06d}"
    _otp_store[req.email.lower()] = code
    await audit.log("otp_requested", req.email.lower(), "auth.request_otp")
    print(f"Chronos dev OTP for {req.email}: {code}", flush=True)
    return {"status": "otp_sent_dev_console"}


@router.post("/verify-otp")
async def verify_otp(req: OtpVerify, response: Response) -> dict[str, str]:
    if settings.cognito_domain:
        raise HTTPException(status_code=404, detail="OTP auth is disabled — use Cognito")
    email = req.email.lower()
    if _otp_store.get(email) != req.code:
        raise HTTPException(status_code=400, detail="Invalid OTP")

    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (await conn.execute(select(members).where(members.c.email == email))).mappings().first()
        if row is None:
            raise HTTPException(status_code=403, detail="Email is not seeded as a Chronos member")
        token = create_access_token(row["id"])
        await conn.execute(
            update(members).where(members.c.id == row["id"]).values(region=settings.region)
        )
    await audit.log("otp_verified", row["id"], "auth.verify_otp")
    response.set_cookie("chronos_session", token, httponly=True, samesite="lax")
    return {"access_token": token, "token_type": "bearer", "member_id": row["id"]}
