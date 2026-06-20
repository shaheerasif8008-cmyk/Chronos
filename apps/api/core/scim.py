from __future__ import annotations
"""
SCIM 2.0 provisioning core (RFC 7643/7644).

An identity provider (Okta, Entra ID, OneLogin, …) calls the /scim/v2 endpoints
with a per-org bearer token to create, update, deactivate, and group users. This
module holds the tenant-correct logic: token auth, resource ↔ member mapping, and
the Users/Groups operations. Only the SHA-256 hash of a SCIM token is stored.

Deprovisioning maps to member lifecycle: ``active: false`` sets the member's
status to ``deactivated`` (login is then refused), rather than hard-deleting, so
audit history and ownership are preserved. Group membership drives Chronos roles:
a member's effective role is the highest role granted by any group they're in.
"""
import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, insert, select, update

from core.db import engine, reflect_table
from core.models import Member

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

# Role precedence (low → high). A member's effective role is the max over their
# base role and all groups they belong to.
_ROLE_RANK = {"viewer": 0, "user": 1, "operator": 2, "manager": 3, "admin": 4, "owner": 5}


def _rank(role: str) -> int:
    return _ROLE_RANK.get(role, 1)


def _max_role(roles: list[str]) -> str:
    return max(roles, key=_rank) if roles else "user"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SCIMError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None):
        self.status = status
        self.detail = detail
        self.scim_type = scim_type
        super().__init__(detail)

    def to_dict(self) -> dict:
        body: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(self.status), "detail": self.detail}
        if self.scim_type:
            body["scimType"] = self.scim_type
        return body


# ── Token auth ───────────────────────────────────────────────────────────────

def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_token() -> str:
    return "scim_" + secrets.token_urlsafe(32)


async def create_token(org_id: str, region: str, *, name: str, default_role: str = "user") -> tuple[dict, str]:
    raw = generate_token()
    table = await reflect_table("scim_tokens")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(table).values(
                    organization_id=org_id, region=region, name=name,
                    token_prefix=raw[:12], token_hash=hash_token(raw),
                    default_role=default_role, enabled=True,
                ).returning(table)
            )
        ).mappings().one()
    return dict(row), raw


async def authenticate_token(raw: str) -> dict | None:
    """Return {org_id, default_role, token_id} for a valid SCIM token, else None."""
    if not raw:
        return None
    table = await reflect_table("scim_tokens")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table).where(table.c.token_hash == hash_token(raw), table.c.enabled.is_(True))
            )
        ).mappings().first()
        if not row:
            return None
        await conn.execute(update(table).where(table.c.id == row["id"]).values(last_used_at=_now()))
    return {"org_id": str(row["organization_id"]), "default_role": str(row["default_role"]), "token_id": str(row["id"])}


# ── User mapping ─────────────────────────────────────────────────────────────

def member_to_scim(member: dict, *, base_url: str) -> dict:
    return {
        "schemas": [USER_SCHEMA],
        "id": str(member["id"]),
        "externalId": member.get("external_id"),
        "userName": member["email"],
        "name": {"formatted": member.get("name") or ""},
        "displayName": member.get("name") or member["email"],
        "emails": [{"value": member["email"], "primary": True}],
        "active": (member.get("status") or "active") == "active",
        "meta": {
            "resourceType": "User",
            "location": f"{base_url}/Users/{member['id']}",
        },
    }


def _extract_email(payload: dict) -> str | None:
    user_name = (payload.get("userName") or "").strip().lower()
    if user_name and "@" in user_name:
        return user_name
    for entry in payload.get("emails") or []:
        value = (entry.get("value") or "").strip().lower()
        if value and (entry.get("primary") or "@" in value):
            return value
    return None


def _extract_name(payload: dict) -> str | None:
    if payload.get("displayName"):
        return payload["displayName"]
    name = payload.get("name") or {}
    if name.get("formatted"):
        return name["formatted"]
    parts = [name.get("givenName"), name.get("familyName")]
    joined = " ".join(p for p in parts if p)
    return joined or None


# ── User operations ──────────────────────────────────────────────────────────

async def list_users(org_id: str, *, filter_expr: str | None, start_index: int, count: int) -> tuple[int, list[dict]]:
    members = await reflect_table("members")
    clauses = [members.c.organization_id == org_id]
    field, value = _parse_filter(filter_expr)
    if field == "userName" and value:
        clauses.append(members.c.email == value.lower())
    elif field == "externalId" and value:
        clauses.append(members.c.external_id == value)
    async with engine.begin() as conn:
        total = (await conn.execute(select(func.count()).select_from(members).where(*clauses))).scalar_one()
        rows = (
            await conn.execute(
                select(members).where(*clauses).order_by(members.c.created_at.asc())
                .offset(max(0, start_index - 1)).limit(count)
            )
        ).mappings().all()
    return int(total), [dict(r) for r in rows]


async def get_user(org_id: str, member_id: str) -> dict | None:
    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(members).where(members.c.organization_id == org_id, members.c.id == member_id)
            )
        ).mappings().first()
    return dict(row) if row else None


async def create_user(org_id: str, region: str, payload: dict, *, default_role: str) -> dict:
    from core.members import provision_member

    email = _extract_email(payload)
    if not email:
        raise SCIMError(400, "userName or a valid email is required", "invalidValue")
    member = await provision_member(
        org_id, email, name=_extract_name(payload), role=default_role,
        external_id=payload.get("externalId"), region=region,
    )
    if payload.get("active") is False:
        await _set_status(org_id, member.id, "deactivated")
    return await get_user(org_id, member.id)  # type: ignore[return-value]


async def replace_user(org_id: str, member_id: str, payload: dict) -> dict | None:
    members = await reflect_table("members")
    existing = await get_user(org_id, member_id)
    if not existing:
        return None
    values: dict = {}
    email = _extract_email(payload)
    if email:
        values["email"] = email
    name = _extract_name(payload)
    if name:
        values["name"] = name
    if payload.get("externalId") is not None:
        values["external_id"] = payload["externalId"]
    if "active" in payload:
        values["status"] = "active" if payload["active"] else "deactivated"
    if values:
        async with engine.begin() as conn:
            await conn.execute(update(members).where(members.c.id == member_id, members.c.organization_id == org_id).values(**values))
    return await get_user(org_id, member_id)


async def patch_user(org_id: str, member_id: str, patch: dict) -> dict | None:
    """Apply a SCIM PatchOp. The common case is Okta/Entra toggling `active`."""
    existing = await get_user(org_id, member_id)
    if not existing:
        return None
    members = await reflect_table("members")
    values: dict = {}
    for op in patch.get("Operations", []):
        operation = (op.get("op") or "").lower()
        path = (op.get("path") or "").lower()
        value = op.get("value")
        if operation == "remove":
            continue
        # value can be a scalar (with path) or a dict of attrs (no path)
        attrs = value if isinstance(value, dict) and not path else {path: value}
        for key, val in attrs.items():
            key = key.lower()
            if key in ("active",):
                active = val if isinstance(val, bool) else str(val).lower() == "true"
                values["status"] = "active" if active else "deactivated"
            elif key in ("username", "emails.value"):
                if isinstance(val, str) and "@" in val:
                    values["email"] = val.lower()
            elif key in ("displayname", "name.formatted"):
                if isinstance(val, str):
                    values["name"] = val
            elif key == "externalid":
                values["external_id"] = val
    if values:
        async with engine.begin() as conn:
            await conn.execute(update(members).where(members.c.id == member_id, members.c.organization_id == org_id).values(**values))
    return await get_user(org_id, member_id)


async def deactivate_user(org_id: str, member_id: str) -> bool:
    """SCIM DELETE → deactivate (soft) so audit/ownership survive."""
    existing = await get_user(org_id, member_id)
    if not existing:
        return False
    await _set_status(org_id, member_id, "deactivated")
    return True


async def _set_status(org_id: str, member_id: str, status: str) -> None:
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(update(members).where(members.c.id == member_id, members.c.organization_id == org_id).values(status=status))


def _parse_filter(expr: str | None) -> tuple[str | None, str | None]:
    """Parse the SCIM filters IdPs actually send: `userName eq "x"`."""
    if not expr:
        return None, None
    parts = expr.split(" ", 2)
    if len(parts) == 3 and parts[1].lower() == "eq":
        return parts[0], parts[2].strip().strip('"')
    return None, None


# ── Group operations + role derivation ───────────────────────────────────────

def group_to_scim(group: dict, members: list[dict], *, base_url: str) -> dict:
    return {
        "schemas": [GROUP_SCHEMA],
        "id": str(group["id"]),
        "externalId": group.get("external_id"),
        "displayName": group["display_name"],
        "members": [{"value": str(m["member_id"]), "display": m.get("email")} for m in members],
        "meta": {"resourceType": "Group", "location": f"{base_url}/Groups/{group['id']}"},
    }


async def list_groups(org_id: str) -> list[dict]:
    table = await reflect_table("scim_groups")
    async with engine.begin() as conn:
        rows = (await conn.execute(select(table).where(table.c.organization_id == org_id))).mappings().all()
    return [dict(r) for r in rows]


async def get_group(org_id: str, group_id: str) -> dict | None:
    table = await reflect_table("scim_groups")
    async with engine.begin() as conn:
        row = (await conn.execute(select(table).where(table.c.organization_id == org_id, table.c.id == group_id))).mappings().first()
    return dict(row) if row else None


async def group_member_rows(org_id: str, group_id: str) -> list[dict]:
    gm = await reflect_table("group_memberships")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(gm.c.member_id, members.c.email)
                .select_from(gm.join(members, members.c.id == gm.c.member_id))
                .where(
                    gm.c.organization_id == org_id,
                    gm.c.group_id == group_id,
                    members.c.organization_id == org_id,
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def create_group(org_id: str, region: str, payload: dict, *, role: str = "user") -> dict:
    table = await reflect_table("scim_groups")
    display = payload.get("displayName")
    if not display:
        raise SCIMError(400, "displayName is required", "invalidValue")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(table).values(
                    organization_id=org_id, region=region, display_name=display,
                    external_id=payload.get("externalId"), role=role,
                ).returning(table)
            )
        ).mappings().one()
    group = dict(row)
    await set_group_members(org_id, group["id"], [m.get("value") for m in payload.get("members", [])])
    return group


async def set_group_members(org_id: str, group_id: str, member_ids: list[str]) -> None:
    gm = await reflect_table("group_memberships")
    members = await reflect_table("members")
    member_ids = [m for m in member_ids if m]
    async with engine.begin() as conn:
        if member_ids:
            valid_ids = set(
                (await conn.execute(
                    select(members.c.id).where(
                        members.c.organization_id == org_id,
                        members.c.id.in_(member_ids),
                    )
                )).scalars().all()
            )
            invalid = sorted(set(member_ids) - {str(mid) for mid in valid_ids})
            if invalid:
                raise SCIMError(400, "Group members must belong to the same organization", "invalidValue")
        existing = (await conn.execute(select(gm.c.member_id).where(gm.c.organization_id == org_id, gm.c.group_id == group_id))).scalars().all()
        await conn.execute(delete(gm).where(gm.c.organization_id == org_id, gm.c.group_id == group_id))
        for mid in member_ids:
            await conn.execute(insert(gm).values(organization_id=org_id, group_id=group_id, member_id=mid))
    affected = set(existing) | set(member_ids)
    for mid in affected:
        await recompute_member_role(org_id, str(mid))


async def patch_group(org_id: str, group_id: str, patch: dict) -> dict | None:
    group = await get_group(org_id, group_id)
    if not group:
        return None
    current = [str(r["member_id"]) for r in await group_member_rows(org_id, group_id)]
    member_set = set(current)
    for op in patch.get("Operations", []):
        operation = (op.get("op") or "").lower()
        path = (op.get("path") or "").lower()
        value = op.get("value")
        if "members" not in path and path:
            continue
        values = value if isinstance(value, list) else ([value] if value else [])
        ids = [v.get("value") if isinstance(v, dict) else v for v in values]
        if operation == "add":
            member_set.update(i for i in ids if i)
        elif operation == "remove":
            if not ids:
                member_set.clear()
            else:
                member_set.difference_update(ids)
        elif operation == "replace":
            member_set = {i for i in ids if i}
    await set_group_members(org_id, group_id, list(member_set))
    return await get_group(org_id, group_id)


async def delete_group(org_id: str, group_id: str) -> bool:
    group = await get_group(org_id, group_id)
    if not group:
        return False
    await set_group_members(org_id, group_id, [])  # detach + recompute roles
    table = await reflect_table("scim_groups")
    async with engine.begin() as conn:
        await conn.execute(delete(table).where(table.c.organization_id == org_id, table.c.id == group_id))
    return True


async def recompute_member_role(org_id: str, member_id: str) -> None:
    """Effective role = max(base 'user', highest role among the member's groups)."""
    gm = await reflect_table("group_memberships")
    groups = await reflect_table("scim_groups")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        roles = (
            await conn.execute(
                select(groups.c.role)
                .select_from(gm.join(groups, groups.c.id == gm.c.group_id))
                .where(gm.c.organization_id == org_id, gm.c.member_id == member_id)
            )
        ).scalars().all()
        effective = _max_role(["user", *list(roles)])
        await conn.execute(update(members).where(members.c.id == member_id, members.c.organization_id == org_id).values(role=effective))
