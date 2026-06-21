from __future__ import annotations
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update

from core import audit
from core.auth import create_access_token, get_current_member, set_session_cookie
from core.models import Member
from core.cognito import (
    CognitoAuthError,
    build_authorize_url,
    cognito_enabled,
    email_from_claims,
    exchange_authorization_code,
    verify_id_token,
)
from core.config import settings
from core.db import engine, reflect_table
from core.invitations import accept_pending_invitation
from core.members import get_member_by_email, get_or_create_member_for_email
from core.signup import signup_or_join

router = APIRouter(prefix="/auth", tags=["auth"])
# email -> {code, expires_at, attempts}. Codes are random, single-use, and
# short-lived; the store is locked after too many failed attempts to defeat
# brute force. Process-local (single-node dev auth); a clustered deployment
# should use a shared store, but dev OTP is disabled in production anyway.
_otp_store: dict[str, dict] = {}
_OTP_TTL_SECONDS = 5 * 60
_OTP_MAX_ATTEMPTS = 5


class OtpRequest(BaseModel):
    email: EmailStr


class OtpVerify(BaseModel):
    email: EmailStr
    code: str


class SignupRequest(BaseModel):
    email: EmailStr
    code: str
    org_name: str | None = Field(default=None, max_length=200)


class CognitoCallbackRequest(BaseModel):
    code: str
    redirect_uri: str | None = None


class CognitoIdTokenRequest(BaseModel):
    id_token: str


def _dev_otp_enabled() -> bool:
    # Hard gate: dev OTP is never available in production, regardless of config
    # (the config guard also refuses to boot with dev_otp in production).
    return (not settings.is_production) and settings.auth_provider in {"dev_otp", "both"}


def _consume_otp(email: str, code: str) -> None:
    """Validate and consume a dev OTP for ``email``. Raises HTTPException on failure."""
    email = email.lower()
    entry = _otp_store.get(email)
    if entry is None or entry["expires_at"] < time.time():
        _otp_store.pop(email, None)
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    if entry["attempts"] >= _OTP_MAX_ATTEMPTS:
        _otp_store.pop(email, None)
        raise HTTPException(status_code=429, detail="Too many attempts; request a new code")
    entry["attempts"] += 1
    if not secrets.compare_digest(code, entry["code"]):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    _otp_store.pop(email, None)


@router.get("/config")
async def auth_config() -> dict:
    """Public auth configuration for the web app."""
    cognito = {
        "enabled": cognito_enabled(),
        "region": settings.cognito_region,
        "userPoolId": settings.cognito_user_pool_id,
        "appClientId": settings.cognito_app_client_id,
        "domain": settings.cognito_domain,
        "callbackUrl": settings.cognito_callback_url,
    }
    if cognito["enabled"]:
        try:
            cognito["loginUrl"] = build_authorize_url()
        except CognitoAuthError:
            cognito["loginUrl"] = None
    return {
        "provider": settings.auth_provider,
        "devOtp": _dev_otp_enabled(),
        "cognito": cognito,
    }


@router.post("/request-otp")
async def request_otp(req: OtpRequest) -> dict[str, str]:
    if not _dev_otp_enabled():
        raise HTTPException(status_code=404, detail="Dev OTP auth is disabled")
    email = req.email.lower()
    code = f"{secrets.randbelow(1_000_000):06d}"
    _otp_store[email] = {
        "code": code,
        "expires_at": time.time() + _OTP_TTL_SECONDS,
        "attempts": 0,
    }
    # Pre-login: no member (and thus no tenant) is resolved yet — the email may
    # not even belong to a seeded member. The process default org is the only
    # org available here, so log it explicitly rather than implying a real tenant.
    await audit.log("otp_requested", email, "auth.request_otp", organization_id=settings.org_id)
    # Dev only: the code is printed to the API console (see CLAUDE.md). It is
    # never returned in the HTTP response, so requesting a code for someone
    # else's email does not disclose it.
    print(f"Chronos dev OTP for {req.email}: {code}", flush=True)
    return {"status": "otp_sent_dev_console"}


@router.post("/verify-otp")
async def verify_otp(req: OtpVerify, response: Response) -> dict[str, str]:
    if not _dev_otp_enabled():
        raise HTTPException(status_code=404, detail="Dev OTP auth is disabled")
    email = req.email.lower()
    _consume_otp(email, req.code)

    member = await get_member_by_email(email)
    if member is None:
        member = await accept_pending_invitation(email, org_id=settings.org_id)
    if member is None:
        raise HTTPException(status_code=403, detail="Email is not seeded as a Chronos member")

    token = create_access_token(member.id)
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(
            update(members).where(members.c.id == member.id).values(region=settings.region)
        )
    await audit.log("otp_verified", member.id, "auth.verify_otp", organization_id=member.organization_id)
    set_session_cookie(response, token)
    return {"access_token": token, "token_type": "bearer", "member_id": member.id}


@router.post("/signup")
async def signup(req: SignupRequest, response: Response) -> dict:
    """Self-serve signup: verify the work email (dev OTP), then create or join an org."""
    if not _dev_otp_enabled():
        raise HTTPException(status_code=404, detail="Self-serve signup requires dev OTP in this environment")
    email = req.email.lower()
    _consume_otp(email, req.code)
    result = await signup_or_join(email, org_name=req.org_name)
    if result.get("status") == "pending_approval":
        return {"status": "pending_approval", "org_id": result["org_id"]}
    # Phase 2A issues a legacy org-less token (grandfathered); Phase 2B flips this
    # to create_access_token(member_id, org_id=result["org_id"]).
    token = create_access_token(result["member_id"])
    set_session_cookie(response, token)
    await audit.log("signup_completed", result["member_id"], "auth.signup",
                    organization_id=result["org_id"], payload={"created": result["created"]})
    return {"access_token": token, "token_type": "bearer", "member_id": result["member_id"],
            "org_id": result["org_id"], "created": result["created"]}


@router.post("/cognito/callback")
async def cognito_callback(req: CognitoCallbackRequest, response: Response) -> dict[str, str]:
    """Exchange Cognito hosted-UI authorization code for a Chronos session JWT."""
    if not cognito_enabled():
        raise HTTPException(status_code=404, detail="Cognito auth is not enabled")
    try:
        tokens = await exchange_authorization_code(req.code, redirect_uri=req.redirect_uri)
        email = email_from_claims(tokens["claims"])
        name = tokens["claims"].get("name")
        member = await get_or_create_member_for_email(email, name=name)
    except CognitoAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    token = create_access_token(member.id)
    await audit.log("cognito_login", member.id, "auth.cognito_callback", organization_id=member.organization_id)
    set_session_cookie(response, token)
    return {"access_token": token, "token_type": "bearer", "member_id": member.id}


@router.post("/cognito/verify")
async def cognito_verify_id_token(req: CognitoIdTokenRequest, response: Response) -> dict[str, str]:
    """Verify a Cognito ID token (e.g. from Amplify) and issue a Chronos session JWT."""
    if not cognito_enabled():
        raise HTTPException(status_code=404, detail="Cognito auth is not enabled")
    try:
        claims = verify_id_token(req.id_token)
        email = email_from_claims(claims)
        member = await get_or_create_member_for_email(email, name=claims.get("name"))
    except CognitoAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    token = create_access_token(member.id)
    await audit.log("cognito_login", member.id, "auth.cognito_verify", organization_id=member.organization_id)
    set_session_cookie(response, token)
    return {"access_token": token, "token_type": "bearer", "member_id": member.id}


@router.get("/me")
async def me(member: Member = Depends(get_current_member)) -> dict:
    """Return the authenticated member's identity. Protected by tenant-binding enforcement."""
    return {"id": member.id, "email": member.email, "role": member.role,
            "organization_id": member.organization_id}
