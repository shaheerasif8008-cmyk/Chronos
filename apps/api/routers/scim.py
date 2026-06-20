from __future__ import annotations
"""
SCIM 2.0 router (RFC 7644).

* ``/scim/v2/*`` — the provisioning protocol, authenticated by a per-org SCIM
  bearer token (an IdP like Okta/Entra calls these). Responses use
  ``application/scim+json`` and SCIM ListResponse/Error envelopes.
* ``/scim/tokens`` — admin management of SCIM tokens (Chronos member auth, audited
  via the permission seam). The raw token is shown exactly once on creation.
"""
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, update

from core import audit, permissions, scim
from core.auth import get_current_member
from core.db import engine, reflect_table
from core.models import Member

router = APIRouter(prefix="/scim", tags=["scim"])

_SCIM_MEDIA = "application/scim+json"


def _scim(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(content=body, status_code=status, media_type=_SCIM_MEDIA)


async def scim_context(authorization: str | None = Header(default=None)) -> dict:
    """Authenticate the SCIM bearer token → tenant context, or raise SCIM 401."""
    raw = ""
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    ctx = await scim.authenticate_token(raw)
    if ctx is None:
        raise scim.SCIMError(401, "Invalid or missing SCIM bearer token")
    return ctx


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/scim/v2"


# ── Discovery ────────────────────────────────────────────────────────────────

@router.get("/v2/ServiceProviderConfig")
async def service_provider_config(_ctx: dict = Depends(scim_context)) -> JSONResponse:
    return _scim({
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "documentationUri": "https://code.claude.com/docs",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{
            "type": "oauthbearertoken", "name": "OAuth Bearer Token",
            "description": "Authentication via the per-org SCIM bearer token.",
        }],
    })


@router.get("/v2/ResourceTypes")
async def resource_types(request: Request, _ctx: dict = Depends(scim_context)) -> JSONResponse:
    base = _base(request)
    types = [
        {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"], "id": "User",
         "name": "User", "endpoint": "/Users", "schema": scim.USER_SCHEMA,
         "meta": {"resourceType": "ResourceType", "location": f"{base}/ResourceTypes/User"}},
        {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"], "id": "Group",
         "name": "Group", "endpoint": "/Groups", "schema": scim.GROUP_SCHEMA,
         "meta": {"resourceType": "ResourceType", "location": f"{base}/ResourceTypes/Group"}},
    ]
    return _scim({"schemas": [scim.LIST_SCHEMA], "totalResults": 2, "Resources": types})


@router.get("/v2/Schemas")
async def schemas(_ctx: dict = Depends(scim_context)) -> JSONResponse:
    return _scim({"schemas": [scim.LIST_SCHEMA], "totalResults": 2,
                  "Resources": [{"id": scim.USER_SCHEMA, "name": "User"},
                                {"id": scim.GROUP_SCHEMA, "name": "Group"}]})


# ── Users ────────────────────────────────────────────────────────────────────

@router.get("/v2/Users")
async def list_users(
    request: Request,
    filter: str | None = Query(default=None),
    startIndex: int = Query(default=1, ge=1),
    count: int = Query(default=100, ge=0, le=200),
    ctx: dict = Depends(scim_context),
) -> JSONResponse:
    total, members = await scim.list_users(ctx["org_id"], filter_expr=filter, start_index=startIndex, count=count)
    base = _base(request)
    resources = [scim.member_to_scim(m, base_url=base) for m in members]
    return _scim({
        "schemas": [scim.LIST_SCHEMA], "totalResults": total,
        "startIndex": startIndex, "itemsPerPage": len(resources), "Resources": resources,
    })


@router.get("/v2/Users/{member_id}")
async def get_user(member_id: str, request: Request, ctx: dict = Depends(scim_context)) -> JSONResponse:
    member = await scim.get_user(ctx["org_id"], member_id)
    if not member:
        raise scim.SCIMError(404, f"User {member_id} not found")
    return _scim(scim.member_to_scim(member, base_url=_base(request)))


@router.post("/v2/Users")
async def create_user(payload: dict, request: Request, ctx: dict = Depends(scim_context)) -> JSONResponse:
    # If the user already exists (same email), SCIM expects 409.
    email = scim._extract_email(payload)
    if email:
        from core.members import get_member_in_org
        existing = await get_member_in_org(ctx["org_id"], email=email)
        if existing:
            raise scim.SCIMError(409, "User already exists", "uniqueness")
    region = await _region_for(ctx["org_id"])
    member = await scim.create_user(ctx["org_id"], region, payload, default_role=ctx["default_role"])
    await audit.log("scim_user_created", ctx["token_id"], "scim.user.create",
                    organization_id=ctx["org_id"], resource_type="member", resource_id=str(member["id"]))
    return _scim(scim.member_to_scim(member, base_url=_base(request)), status=201)


@router.put("/v2/Users/{member_id}")
async def replace_user(member_id: str, payload: dict, request: Request, ctx: dict = Depends(scim_context)) -> JSONResponse:
    member = await scim.replace_user(ctx["org_id"], member_id, payload)
    if not member:
        raise scim.SCIMError(404, f"User {member_id} not found")
    await audit.log("scim_user_updated", ctx["token_id"], "scim.user.replace",
                    organization_id=ctx["org_id"], resource_type="member", resource_id=member_id)
    return _scim(scim.member_to_scim(member, base_url=_base(request)))


@router.patch("/v2/Users/{member_id}")
async def patch_user(member_id: str, patch: dict, request: Request, ctx: dict = Depends(scim_context)) -> JSONResponse:
    member = await scim.patch_user(ctx["org_id"], member_id, patch)
    if not member:
        raise scim.SCIMError(404, f"User {member_id} not found")
    await audit.log("scim_user_patched", ctx["token_id"], "scim.user.patch",
                    organization_id=ctx["org_id"], resource_type="member", resource_id=member_id)
    return _scim(scim.member_to_scim(member, base_url=_base(request)))


@router.delete("/v2/Users/{member_id}")
async def delete_user(member_id: str, ctx: dict = Depends(scim_context)) -> JSONResponse:
    ok = await scim.deactivate_user(ctx["org_id"], member_id)
    if not ok:
        raise scim.SCIMError(404, f"User {member_id} not found")
    await audit.log("scim_user_deactivated", ctx["token_id"], "scim.user.delete",
                    organization_id=ctx["org_id"], resource_type="member", resource_id=member_id)
    return JSONResponse(content=None, status_code=204, media_type=_SCIM_MEDIA)


# ── Groups ───────────────────────────────────────────────────────────────────

@router.get("/v2/Groups")
async def list_groups(request: Request, ctx: dict = Depends(scim_context)) -> JSONResponse:
    base = _base(request)
    groups = await scim.list_groups(ctx["org_id"])
    resources = [scim.group_to_scim(g, await scim.group_member_rows(ctx["org_id"], g["id"]), base_url=base) for g in groups]
    return _scim({"schemas": [scim.LIST_SCHEMA], "totalResults": len(resources),
                  "startIndex": 1, "itemsPerPage": len(resources), "Resources": resources})


@router.get("/v2/Groups/{group_id}")
async def get_group(group_id: str, request: Request, ctx: dict = Depends(scim_context)) -> JSONResponse:
    group = await scim.get_group(ctx["org_id"], group_id)
    if not group:
        raise scim.SCIMError(404, f"Group {group_id} not found")
    rows = await scim.group_member_rows(ctx["org_id"], group_id)
    return _scim(scim.group_to_scim(group, rows, base_url=_base(request)))


@router.post("/v2/Groups")
async def create_group(payload: dict, request: Request, ctx: dict = Depends(scim_context)) -> JSONResponse:
    region = await _region_for(ctx["org_id"])
    group = await scim.create_group(ctx["org_id"], region, payload)
    rows = await scim.group_member_rows(ctx["org_id"], group["id"])
    await audit.log("scim_group_created", ctx["token_id"], "scim.group.create",
                    organization_id=ctx["org_id"], resource_type="scim_group", resource_id=str(group["id"]))
    return _scim(scim.group_to_scim(group, rows, base_url=_base(request)), status=201)


@router.patch("/v2/Groups/{group_id}")
async def patch_group(group_id: str, patch: dict, request: Request, ctx: dict = Depends(scim_context)) -> JSONResponse:
    group = await scim.patch_group(ctx["org_id"], group_id, patch)
    if not group:
        raise scim.SCIMError(404, f"Group {group_id} not found")
    rows = await scim.group_member_rows(ctx["org_id"], group_id)
    await audit.log("scim_group_patched", ctx["token_id"], "scim.group.patch",
                    organization_id=ctx["org_id"], resource_type="scim_group", resource_id=group_id)
    return _scim(scim.group_to_scim(group, rows, base_url=_base(request)))


@router.delete("/v2/Groups/{group_id}")
async def delete_group(group_id: str, ctx: dict = Depends(scim_context)) -> JSONResponse:
    ok = await scim.delete_group(ctx["org_id"], group_id)
    if not ok:
        raise scim.SCIMError(404, f"Group {group_id} not found")
    await audit.log("scim_group_deleted", ctx["token_id"], "scim.group.delete",
                    organization_id=ctx["org_id"], resource_type="scim_group", resource_id=group_id)
    return JSONResponse(content=None, status_code=204, media_type=_SCIM_MEDIA)


# ── Admin: SCIM token management (Chronos member auth) ───────────────────────

class SCIMTokenInput(BaseModel):
    name: str = "Provisioning token"
    default_role: str = "user"


@router.get("/tokens")
async def list_tokens(member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "manage_scim", member.organization_id)
    table = await reflect_table("scim_tokens")
    async with engine.begin() as conn:
        rows = (await conn.execute(select(table).where(table.c.organization_id == member.organization_id))).mappings().all()
    return [{k: v for k, v in dict(r).items() if k != "token_hash"} for r in rows]


@router.post("/tokens")
async def create_scim_token(req: SCIMTokenInput, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "manage_scim", member.organization_id)
    row, raw = await scim.create_token(member.organization_id, member.region, name=req.name, default_role=req.default_role)
    await audit.log("scim_token_created", member.id, "scim.token.create",
                    organization_id=member.organization_id, resource_type="scim_token", resource_id=str(row["id"]))
    public = {k: v for k, v in row.items() if k != "token_hash"}
    # The raw token is returned exactly once; only its hash is persisted.
    return {**public, "token": raw, "scim_base_url": "/scim/v2"}


@router.delete("/tokens/{token_id}")
async def revoke_scim_token(token_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "manage_scim", member.organization_id)
    table = await reflect_table("scim_tokens")
    async with engine.begin() as conn:
        result = await conn.execute(
            update(table).where(table.c.id == token_id, table.c.organization_id == member.organization_id).values(enabled=False)
        )
    if result.rowcount == 0:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Token not found")
    await audit.log("scim_token_revoked", member.id, "scim.token.revoke",
                    organization_id=member.organization_id, resource_type="scim_token", resource_id=token_id)
    return {"status": "revoked", "token_id": token_id}


async def _region_for(org_id: str) -> str:
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        row = (await conn.execute(select(orgs.c.region).where(orgs.c.id == org_id))).first()
    return str(row[0]) if row and row[0] else "us"
