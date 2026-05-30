from __future__ import annotations
import random

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, update

from core import audit
from core.auth import create_access_token
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
from core.members import get_member_by_email, get_or_create_member_for_email

router = APIRouter(prefix="/auth", tags=["auth"])
_otp_store: dict[str, str] = {}


class OtpRequest(BaseModel):
    email: EmailStr


class OtpVerify(BaseModel):
    email: EmailStr
    code: str


class CognitoCallbackRequest(BaseModel):
    code: str
    redirect_uri: str | None = None


class CognitoIdTokenRequest(BaseModel):
    id_token: str


def _dev_otp_enabled() -> bool:
    return settings.auth_provider in {"dev_otp", "both"}


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
    code = f"{random.randint(0, 999999):06d}"
    _otp_store[req.email.lower()] = code
    await audit.log("otp_requested", req.email.lower(), "auth.request_otp")
    print(f"Chronos dev OTP for {req.email}: {code}", flush=True)
    return {"status": "otp_sent_dev_console"}


@router.post("/verify-otp")
async def verify_otp(req: OtpVerify, response: Response) -> dict[str, str]:
    if not _dev_otp_enabled():
        raise HTTPException(status_code=404, detail="Dev OTP auth is disabled")
    email = req.email.lower()
    if _otp_store.get(email) != req.code:
        raise HTTPException(status_code=400, detail="Invalid OTP")

    member = await get_member_by_email(email)
    if member is None:
        raise HTTPException(status_code=403, detail="Email is not seeded as a Chronos member")

    token = create_access_token(member.id)
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(
            update(members).where(members.c.id == member.id).values(region=settings.region)
        )
    await audit.log("otp_verified", member.id, "auth.verify_otp")
    response.set_cookie("chronos_session", token, httponly=True, samesite="lax")
    return {"access_token": token, "token_type": "bearer", "member_id": member.id}


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
    await audit.log("cognito_login", member.id, "auth.cognito_callback")
    response.set_cookie("chronos_session", token, httponly=True, samesite="lax")
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
    await audit.log("cognito_login", member.id, "auth.cognito_verify")
    response.set_cookie("chronos_session", token, httponly=True, samesite="lax")
    return {"access_token": token, "token_type": "bearer", "member_id": member.id}
