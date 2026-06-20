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

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, insert, select, update

from core import audit, permissions, sso
from core.auth import create_access_token, get_current_member, set_session_cookie
from core.config import settings
from core.db import engine, reflect_table
from core.members import provision_member
from core.models import Member

router = APIRouter(prefix="/auth/sso", tags=["sso"])


class SSOConnectionInput(BaseModel):
    issuer: str
    client_id: str
    client_secret: str = ""
    display_name: str = ""
    email_domain: str | None = None
    default_role: str = "user"
    scopes: str = "openid email profile"
    authorize_url: str = ""
    token_url: str = ""
    jwks_url: str = ""
    userinfo_url: str = ""
    enabled: bool = True


# ── Public login ─────────────────────────────────────────────────────────────

@router.get("/start")
async def sso_start(
    email: str = Query(..., description="User email; routes to the IdP by domain"),
    redirect: str = Query(default="/chat"),
) -> dict:
    conn = await sso.get_connection_by_domain(email)
    if conn is None:
        raise HTTPException(status_code=404, detail="No SSO configured for this email domain")
    try:
        login_url = await sso.build_login_url(conn, redirect=redirect, nonce=secrets.token_urlsafe(16))
    except sso.SSOError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"login_url": login_url}


@router.get("/callback")
async def sso_callback(
    code: str = Query(...),
    state: str = Query(...),
):
    try:
        claims_state = sso.verify_state(state)
        conn = await sso.get_connection_by_id(claims_state["cid"])
        if conn is None or not conn.enabled:
            raise sso.SSOError("SSO connection is no longer available")
        claims = await sso.exchange_code(conn, code)
        email = sso.email_from_claims(claims)
        member = await provision_member(
            conn.organization_id, email,
            name=claims.get("name") or claims.get("given_name"),
            role=conn.default_role,
            external_id=claims.get("sub"),
            sso_subject=claims.get("sub"),
        )
    except sso.SSOError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    token = create_access_token(member.id)
    await audit.log("sso_login", member.id, "auth.sso_callback",
                    organization_id=member.organization_id,
                    payload={"connection_id": conn.id})

    redirect_path = claims_state.get("redirect") or "/chat"
    target = f"{settings.frontend_base_url.rstrip('/')}/login/callback#access_token={token}&redirect={redirect_path}"
    response = RedirectResponse(url=target, status_code=302)
    set_session_cookie(response, token)
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
    if req.email_domain:
        values["email_domain"] = req.email_domain.lower().strip()
    async with engine.begin() as conn:
        row = (await conn.execute(insert(table).values(**values).returning(table))).mappings().one()
    await audit.log("sso_connection_created", member.id, "sso.create",
                    organization_id=member.organization_id, resource_type="sso_connection",
                    resource_id=str(row["id"]), payload={"issuer": req.issuer})
    return _public(dict(row))


@router.patch("/connections/{connection_id}")
async def update_connection(connection_id: str, req: SSOConnectionInput, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "manage_sso", member.organization_id)
    table = await reflect_table("sso_connections")
    values = {**req.model_dump(), "region": member.region}
    if req.email_domain:
        values["email_domain"] = req.email_domain.lower().strip()
    async with engine.begin() as conn:
        row = (await conn.execute(
            update(table)
            .where(table.c.id == connection_id, table.c.organization_id == member.organization_id)
            .values(**values).returning(table)
        )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Connection not found")
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
