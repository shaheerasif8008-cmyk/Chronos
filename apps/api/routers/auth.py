from __future__ import annotations
import secrets
import time

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update

from core import audit
from core.auth import (
    clear_session_cookie,
    create_access_token,
    create_cognito_oauth_state,
    decode_cognito_oauth_state,
    get_current_member,
    set_session_cookie,
)
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
from core.invitations import accept_pending_invitation, resolve_invitation
from core.members import get_member_by_email, get_member_in_org
from core.signup import signup_or_join
from core.tenancy import extract_tenant_label, resolve_org_id

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
    state: str | None = None


class CognitoIdTokenRequest(BaseModel):
    id_token: str
    state: str | None = None


@router.get("/invitations/{token}")
async def invitation_login_context(token: str) -> dict:
    """Resolve an opaque invite link for the login screen.

    The 256-bit bearer token is stored only as a digest. A valid link reveals
    just the intended recipient/workspace routing data and never creates a
    session; the recipient still has to authenticate as the invited email.
    """

    invitation = await resolve_invitation(token)
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation is invalid or expired")
    return invitation


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


async def _resolve_cognito_member(email: str, *, name: str | None, resolved_org_id: str | None):
    """Resolve a Cognito-verified email to a member. Per-subdomain: log into the
    resolved org if already a member there. At apex, an existing default-org member
    logs in. Otherwise self-serve create/join via signup_or_join — but ONLY when
    auto-provisioning is enabled (preserves the cognito_auto_provision_members gate)."""
    email = email.lower()
    if resolved_org_id is not None:
        member = await get_member_in_org(resolved_org_id, email=email)
        if member is not None:
            return member
        member = await accept_pending_invitation(email, org_id=resolved_org_id)
        if member is not None:
            return member
    else:
        member = await get_member_by_email(email)  # apex: existing default-org member
        if member is not None:
            return member
    if not settings.cognito_auto_provision_members:
        raise HTTPException(status_code=403, detail="Email is not registered as a Chronos member")
    result = await signup_or_join(email, org_name=name)
    if resolved_org_id is not None and str(result.get("org_id")) != str(resolved_org_id):
        raise HTTPException(status_code=403, detail="Email is not registered for this organization")
    if result.get("member_id") is None:
        raise HTTPException(status_code=403, detail="Membership pending approval")
    return await get_member_in_org(result["org_id"], email=email)


def _set_cognito_state_cookie(response: Response, state: str) -> None:
    response.set_cookie(
        "chronos_oauth_state",
        state,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=10 * 60,
    )


def _clear_cognito_state_cookie(response: Response) -> None:
    response.delete_cookie(
        "chronos_oauth_state",
        path="/",
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
    )


async def _tenant_from_state(state: str) -> tuple[str, str]:
    try:
        claims = decode_cognito_oauth_state(state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(organizations.c.id, organizations.c.subdomain).where(
                    organizations.c.id == claims["org"],
                    organizations.c.subdomain == claims["tenant"],
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=400, detail="Cognito login organization is no longer available")
    return str(row["id"]), str(row["subdomain"])


@router.get("/config")
async def auth_config(request: Request, response: Response, tenant: str | None = None) -> dict:
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
            state: str | None = None
            tenant_label: str | None = None
            resolved_org_id = getattr(request.state, "resolved_org_id", None)
            if tenant:
                tenant_label = extract_tenant_label(
                    f"{tenant.strip().lower()}.{settings.base_domain}",
                    base_domain=settings.base_domain,
                )
                if tenant_label is None:
                    raise HTTPException(status_code=404, detail="Organization not found")
                resolved_org_id = await resolve_org_id(
                    f"{tenant_label}.{settings.base_domain}",
                    None,
                )
                if resolved_org_id is None:
                    raise HTTPException(status_code=404, detail="Organization not found")
            elif resolved_org_id is not None:
                organizations = await reflect_table("organizations")
                async with engine.begin() as conn:
                    tenant_label = (
                        await conn.execute(
                            select(organizations.c.subdomain).where(
                                organizations.c.id == resolved_org_id
                            )
                        )
                    ).scalar_one_or_none()

            if resolved_org_id is not None and tenant_label:
                state = create_cognito_oauth_state(
                    org_id=str(resolved_org_id),
                    subdomain=str(tenant_label),
                )
                _set_cognito_state_cookie(response, state)
            # Production Cognito login must be tenant-bound. Local development
            # keeps the unscoped URL for its single-host workflow.
            cognito["loginUrl"] = (
                build_authorize_url(state=state)
                if state is not None or not settings.is_production
                else None
            )
            cognito["requiresTenant"] = settings.is_production
            cognito["tenant"] = tenant_label
        except CognitoAuthError:
            cognito["loginUrl"] = None
    return {
        "provider": settings.auth_provider,
        "devOtp": _dev_otp_enabled(),
        "cognito": cognito,
        "publicLinks": {
            "terms": settings.terms_url or None,
            "privacy": settings.privacy_url or None,
            "support": settings.support_url or None,
            "status": settings.status_url or None,
        },
        "sessionCookie": {
            "essential": True,
            "httpOnly": True,
            "secure": settings.is_production,
            "sameSite": "lax",
            "advertising": False,
        },
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
async def verify_otp(req: OtpVerify, request: Request, response: Response) -> dict[str, str]:
    if not _dev_otp_enabled():
        raise HTTPException(status_code=404, detail="Dev OTP auth is disabled")
    email = req.email.lower()
    _consume_otp(email, req.code)

    # Per-subdomain login: resolve the member in the request's tenant. Apex / no
    # tenant context falls back to the default org (dev convenience).
    resolved = getattr(request.state, "resolved_org_id", None)
    if resolved is not None:
        member = await get_member_in_org(resolved, email=email)
        if member is None:
            member = await accept_pending_invitation(email, org_id=resolved)
    else:
        member = await get_member_by_email(email)
        if member is None:
            member = await accept_pending_invitation(email, org_id=settings.org_id)
    if member is None:
        raise HTTPException(status_code=403, detail="Email is not a member of this organization")

    token = create_access_token(member.id, org_id=member.organization_id)
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(update(members).where(members.c.id == member.id).values(region=settings.region))
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
    # Phase 2B: token is now org-bound (org_id claim set to the resolved org).
    token = create_access_token(result["member_id"], org_id=result["org_id"])
    set_session_cookie(response, token)
    await audit.log("signup_completed", result["member_id"], "auth.signup",
                    organization_id=result["org_id"], payload={"created": result["created"]})
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        subdomain = (await conn.execute(
            select(organizations.c.subdomain).where(organizations.c.id == result["org_id"])
        )).scalar_one()
    return {"access_token": token, "token_type": "bearer", "member_id": result["member_id"],
            "org_id": result["org_id"], "subdomain": subdomain, "created": result["created"]}


@router.post("/cognito/callback")
async def cognito_callback(
    req: CognitoCallbackRequest,
    request: Request,
    response: Response,
    chronos_oauth_state: str | None = Cookie(default=None),
) -> dict[str, str]:
    """Exchange Cognito hosted-UI authorization code for a Chronos session JWT."""
    if not cognito_enabled():
        raise HTTPException(status_code=404, detail="Cognito auth is not enabled")
    resolved_org_id = getattr(request.state, "resolved_org_id", None)
    tenant_label: str | None = None
    if req.state:
        if settings.is_production and (
            not chronos_oauth_state
            or not secrets.compare_digest(req.state, chronos_oauth_state)
        ):
            raise HTTPException(status_code=400, detail="Cognito login state did not match this browser")
        resolved_org_id, tenant_label = await _tenant_from_state(req.state)
    elif settings.is_production:
        raise HTTPException(status_code=400, detail="Cognito login state is required")

    redirect_uri = req.redirect_uri or settings.cognito_callback_url
    if settings.is_production and not secrets.compare_digest(
        redirect_uri.rstrip("/"), settings.cognito_callback_url.rstrip("/")
    ):
        raise HTTPException(status_code=400, detail="Invalid Cognito callback URL")
    try:
        tokens = await exchange_authorization_code(req.code, redirect_uri=redirect_uri)
        email = email_from_claims(tokens["claims"])
        name = tokens["claims"].get("name")
        member = await _resolve_cognito_member(
            email, name=name, resolved_org_id=resolved_org_id
        )
    except CognitoAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    token = create_access_token(member.id, org_id=member.organization_id)
    await audit.log("cognito_login", member.id, "auth.cognito_callback", organization_id=member.organization_id)
    set_session_cookie(response, token)
    _clear_cognito_state_cookie(response)
    result = {"access_token": token, "token_type": "bearer", "member_id": member.id}
    if tenant_label:
        result["redirect_url"] = f"https://{tenant_label}.{settings.base_domain}/chat"
    return result


@router.post("/cognito/verify")
async def cognito_verify_id_token(req: CognitoIdTokenRequest, request: Request, response: Response) -> dict[str, str]:
    """Verify a Cognito ID token (e.g. from Amplify) and issue a Chronos session JWT."""
    if not cognito_enabled():
        raise HTTPException(status_code=404, detail="Cognito auth is not enabled")
    resolved_org_id = getattr(request.state, "resolved_org_id", None)
    if req.state:
        resolved_org_id, _ = await _tenant_from_state(req.state)
    elif settings.is_production:
        raise HTTPException(status_code=400, detail="Cognito login state is required")
    try:
        claims = verify_id_token(req.id_token)
        email = email_from_claims(claims)
        member = await _resolve_cognito_member(
            email, name=claims.get("name"), resolved_org_id=resolved_org_id
        )
    except CognitoAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    token = create_access_token(member.id, org_id=member.organization_id)
    await audit.log("cognito_login", member.id, "auth.cognito_verify", organization_id=member.organization_id)
    set_session_cookie(response, token)
    return {"access_token": token, "token_type": "bearer", "member_id": member.id}


@router.get("/me")
async def me(member: Member = Depends(get_current_member)) -> dict:
    """Return the authenticated member's identity. Protected by tenant-binding enforcement."""
    return {"id": member.id, "email": member.email, "role": member.role,
            "organization_id": member.organization_id}


@router.post("/logout")
async def logout(response: Response, member: Member = Depends(get_current_member)) -> dict[str, str]:
    """Revoke the browser session cookie for the current client."""

    clear_session_cookie(response)
    await audit.log("session_logout", member.id, "auth.logout", organization_id=member.organization_id)
    return {"status": "signed_out"}
