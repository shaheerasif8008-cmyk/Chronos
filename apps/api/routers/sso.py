from __future__ import annotations
"""
Enterprise SSO router — OIDC login + per-org connection management.

Login flow:
  GET  /auth/sso/start?email=  → resolve the IdP by email domain, return login_url
  GET  /auth/sso/callback      → IdP redirects here; exchange code, JIT-provision
                                 the member, issue a Chronos session, bounce to web

Admin (org admins, audited via the permission seam):
  GET/POST/PATCH/DELETE /auth/sso/connections
"""
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, insert, select, update

from core import audit, permissions, sso
from core.domains import is_domain_hard_claimed
from core.auth import create_access_token, get_current_member, set_session_cookie
from core.config import settings
from core.db import engine, reflect_table
from core.members import provision_member
from core.models import Member

router = APIRouter(prefix="/auth/sso", tags=["sso"])


class SSOConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer: str
    client_id: str
    client_secret: str = ""
    display_name: str = ""
    email_domain: str | None = None
    default_role: str = "viewer"
    scopes: str = "openid email profile"
    authorize_url: str = ""
    token_url: str = ""
    jwks_url: str = ""
    userinfo_url: str = ""
    enabled: bool = True


class SSOConnectionPatch(BaseModel):
    """Partial update. An omitted or blank secret always preserves the old one."""

    model_config = ConfigDict(extra="forbid")

    issuer: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    display_name: str | None = None
    email_domain: str | None = None
    default_role: str | None = None
    scopes: str | None = None
    authorize_url: str | None = None
    token_url: str | None = None
    jwks_url: str | None = None
    userinfo_url: str | None = None
    enabled: bool | None = None


_SSO_DEFAULT_ROLES = {"viewer", "operator", "manager", "admin", "user"}


def _safe_redirect_path(value: str) -> str:
    return (
        value
        if value.startswith("/") and not value.startswith(("//", "/\\"))
        else "/chat"
    )


def _set_sso_state_cookie(response: Response, state: str) -> None:
    response.set_cookie(
        "chronos_sso_state",
        state,
        path="/auth/sso/callback",
        domain=f".{settings.base_domain}" if settings.is_production else None,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=10 * 60,
    )


def _clear_sso_state_cookie(response: Response) -> None:
    response.delete_cookie(
        "chronos_sso_state",
        path="/auth/sso/callback",
        domain=f".{settings.base_domain}" if settings.is_production else None,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
    )


def _validate_connection_values(values: dict) -> None:
    try:
        sso.validate_connection_shape(values)
    except sso.SSOError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    role = str(values.get("default_role") or "viewer")
    if role not in _SSO_DEFAULT_ROLES:
        raise HTTPException(status_code=400, detail="Invalid default SSO role")
    if not str(values.get("client_id") or "").strip():
        raise HTTPException(status_code=400, detail="OIDC client ID is required")
    scopes = {part for part in str(values.get("scopes") or "").split() if part}
    if "openid" not in scopes:
        raise HTTPException(status_code=400, detail="OIDC scopes must include openid")


# ── Public login ─────────────────────────────────────────────────────────────

@router.get("/start")
async def sso_start(
    response: Response,
    email: str = Query(
        ..., max_length=320, description="User email; routes to the IdP by domain"
    ),
    redirect: str = Query(default="/chat", max_length=2048),
) -> dict:
    conn = await sso.get_connection_by_domain(email)
    if conn is None:
        raise HTTPException(status_code=404, detail="No SSO configured for this email domain")
    redirect = _safe_redirect_path(redirect)
    nonce = secrets.token_urlsafe(24)
    state = sso.sign_state(conn.id, conn.organization_id, redirect, nonce)
    try:
        login_url = await sso.build_login_url(
            conn, redirect=redirect, nonce=nonce, state=state
        )
    except sso.SSOError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    _set_sso_state_cookie(response, state)
    return {"login_url": login_url}


@router.get("/callback")
async def sso_callback(
    code: str = Query(...),
    state: str = Query(...),
    chronos_sso_state: str | None = Cookie(default=None),
):
    try:
        if not chronos_sso_state or not secrets.compare_digest(state, chronos_sso_state):
            raise sso.SSOError("SSO login state did not match this browser")
        claims_state = sso.verify_state(state)
        conn = await sso.get_connection_by_id(
            claims_state["cid"], organization_id=claims_state["org"]
        )
        if conn is None or not conn.enabled:
            raise sso.SSOError("SSO connection is no longer available")
        claims = await sso.exchange_code(
            conn, code, expected_nonce=str(claims_state["nonce"])
        )
        email = sso.email_from_claims(claims)
        if claims.get("email_verified") is False:
            raise sso.SSOError("IdP did not verify the email address")
        if conn.email_domain and email.rsplit("@", 1)[-1] != conn.email_domain.lower():
            raise sso.SSOError("IdP email domain did not match this SSO connection")
        member = await provision_member(
            conn.organization_id, email,
            name=claims.get("name") or claims.get("given_name"),
            role="viewer" if conn.default_role == "user" else conn.default_role,
            external_id=claims.get("sub"),
            sso_subject=claims.get("sub"),
        )
    except sso.SSOError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    token = create_access_token(member.id, org_id=member.organization_id)
    await audit.log("sso_login", member.id, "auth.sso_callback",
                    organization_id=member.organization_id,
                    payload={"connection_id": conn.id})

    redirect_path = claims_state.get("redirect") or "/chat"
    # Only a same-site path is safe. A bare startswith("/") check still admits
    # protocol-relative targets like "//evil.com" or "/\evil.com", which the web
    # app would follow as an external navigation (open redirect). Require a single
    # leading slash not followed by another slash or backslash.
    safe_redirect = (
        _safe_redirect_path(redirect_path)
        if isinstance(redirect_path, str)
        else "/chat"
    )
    frontend_base = settings.frontend_base_url.rstrip("/")
    if settings.is_production:
        organizations = await reflect_table("organizations")
        async with engine.begin() as db:
            tenant = (
                await db.execute(
                    select(organizations.c.subdomain).where(
                        organizations.c.id == conn.organization_id
                    )
                )
            ).scalar_one_or_none()
        if not tenant:
            raise HTTPException(status_code=400, detail="SSO organization is no longer available")
        frontend_base = f"https://{tenant}.{settings.base_domain}"
    target = f"{frontend_base}/login/callback?{urlencode({'redirect': safe_redirect})}"
    response = RedirectResponse(url=target, status_code=302)
    set_session_cookie(response, token)
    _clear_sso_state_cookie(response)
    return response


# ── Admin: connection management ─────────────────────────────────────────────

def _public(row: dict) -> dict:
    """Never expose the client secret over the API."""
    out = {k: v for k, v in row.items() if k != "client_secret"}
    out["has_client_secret"] = bool(row.get("client_secret"))
    return out


@router.get("/connections")
async def list_connections(member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "manage_sso", member.organization_id)
    table = await reflect_table("sso_connections")
    async with engine.begin() as conn:
        rows = (await conn.execute(
            select(table).where(table.c.organization_id == member.organization_id).order_by(table.c.created_at.asc())
        )).mappings().all()
    return [_public(dict(r)) for r in rows]


@router.post("/connections")
async def create_connection(req: SSOConnectionInput, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "manage_sso", member.organization_id)
    table = await reflect_table("sso_connections")
    values = {**req.model_dump(), "organization_id": member.organization_id, "region": member.region}
    _validate_connection_values(values)
    try:
        values["client_secret"] = sso.protect_client_secret(
            str(values.get("client_secret") or ""),
            organization_id=member.organization_id,
        )
    except sso.SSOError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if req.email_domain:
        domain = req.email_domain.lower().strip()
        if not await is_domain_hard_claimed(member.organization_id, domain):
            raise HTTPException(status_code=403,
                                detail="Domain must be DNS-verified by this org before configuring SSO")
        values["email_domain"] = domain
    async with engine.begin() as conn:
        row = (await conn.execute(insert(table).values(**values).returning(table))).mappings().one()
    await audit.log("sso_connection_created", member.id, "sso.create",
                    organization_id=member.organization_id, resource_type="sso_connection",
                    resource_id=str(row["id"]), payload={"issuer": req.issuer})
    return _public(dict(row))


@router.patch("/connections/{connection_id}")
async def update_connection(connection_id: str, req: SSOConnectionPatch, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "manage_sso", member.organization_id)
    table = await reflect_table("sso_connections")
    values = req.model_dump(exclude_unset=True)
    # Secrets are write-only. The UI intentionally omits the stored value and
    # older clients sent an empty string on every PATCH; both must preserve it.
    if not values.get("client_secret"):
        values.pop("client_secret", None)
    if "email_domain" in values and values["email_domain"]:
        domain = str(values["email_domain"]).lower().strip()
        if not await is_domain_hard_claimed(member.organization_id, domain):
            raise HTTPException(status_code=403,
                                detail="Domain must be DNS-verified by this org before configuring SSO")
        values["email_domain"] = domain
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                select(table).where(
                    table.c.id == connection_id,
                    table.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
        if not existing:
            raise HTTPException(status_code=404, detail="Connection not found")
        merged = {**dict(existing), **values}
        _validate_connection_values(merged)
        if "client_secret" in values:
            try:
                values["client_secret"] = sso.protect_client_secret(
                    str(values["client_secret"]),
                    organization_id=member.organization_id,
                )
            except sso.SSOError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        row = (await conn.execute(
            update(table)
            .where(table.c.id == connection_id, table.c.organization_id == member.organization_id)
            .values(**values, region=member.region).returning(table)
        )).mappings().first()
    await audit.log("sso_connection_updated", member.id, "sso.update",
                    organization_id=member.organization_id, resource_type="sso_connection",
                    resource_id=connection_id)
    return _public(dict(row))


@router.delete("/connections/{connection_id}")
async def delete_connection(connection_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "manage_sso", member.organization_id)
    table = await reflect_table("sso_connections")
    async with engine.begin() as conn:
        result = await conn.execute(
            delete(table).where(table.c.id == connection_id, table.c.organization_id == member.organization_id)
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Connection not found")
    await audit.log("sso_connection_deleted", member.id, "sso.delete",
                    organization_id=member.organization_id, resource_type="sso_connection",
                    resource_id=connection_id)
    return {"status": "deleted", "connection_id": connection_id}
