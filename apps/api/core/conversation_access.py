"""Canonical shared-conversation ACL policy and persistence.

The conversation owner is immutable (``conversations.member_id``) and every
other grant lives in ``conversation_members``.  All helpers require both the
conversation and ACL row to match the caller's organization, so knowing a UUID
never turns an organization peer into a reader.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql.elements import ColumnElement

from core.db import engine, reflect_table
from core.models import Member


# Conversation transcripts were historically creator-private even from org
# administrators. Explicit ACLs are the only sharing path; compliance exports
# remain a separate, audited control rather than an implicit read bypass.
ADMIN_ROLES: set[str] = set()
ACL_ROLES = {"owner", "editor", "viewer"}
SHAREABLE_ROLES = {"editor", "viewer"}
_ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3, "admin": 4}


def role_allows(role: str | None, minimum: str) -> bool:
    return _ROLE_RANK.get(str(role or ""), 0) >= _ROLE_RANK[minimum]


def visibility_clause(
    conversations: Any,
    conversation_members: Any,
    member: Member,
    *,
    select_fn: Any = select,
) -> ColumnElement[bool]:
    """SQL predicate for conversations visible to ``member``.

    Callers must add their ordinary organization predicate separately.  Admins
    are intentionally handled by the caller because returning ``true()`` here
    would hide accidental omissions of tenant scoping during review.
    """

    membership = select_fn(conversation_members.c.id).where(
        conversation_members.c.organization_id == member.organization_id,
        conversation_members.c.conversation_id == conversations.c.id,
        conversation_members.c.member_id == member.id,
        conversation_members.c.role.in_(ACL_ROLES),
    )
    return or_(
        conversations.c.member_id == member.id,
        membership.exists(),
    )


async def access_in_connection(
    conn: Any,
    conversations: Any,
    conversation_members: Any | None,
    *,
    member: Member,
    conversation_id: str,
    select_fn: Any = select,
) -> tuple[dict[str, Any] | None, str | None]:
    """Load one tenant-validated conversation and its caller role.

    ``conversation_members`` may be ``None`` during a rolling deploy before the
    migration has reached a replica.  The fallback is owner/admin-only, which is
    fail-closed and preserves the historical private behavior.
    """

    if conversation_members is None:
        conditions = [
            conversations.c.id == conversation_id,
            conversations.c.organization_id == member.organization_id,
        ]
        if member.role not in ADMIN_ROLES:
            conditions.append(conversations.c.member_id == member.id)
        row = (
            await conn.execute(select_fn(conversations).where(*conditions))
        ).mappings().first()
        if row is None:
            return None, None
        return dict(row), "admin" if member.role in ADMIN_ROLES else "owner"

    acl_join = conversations.outerjoin(
        conversation_members,
        and_(
            conversation_members.c.organization_id == conversations.c.organization_id,
            conversation_members.c.conversation_id == conversations.c.id,
            conversation_members.c.member_id == member.id,
        ),
    )
    stmt = (
        select_fn(
            *conversations.c,
            conversation_members.c.role.label("_access_role"),
        )
        .select_from(acl_join)
        .where(
            conversations.c.id == conversation_id,
            conversations.c.organization_id == member.organization_id,
        )
    )
    if member.role not in ADMIN_ROLES:
        stmt = stmt.where(
            or_(
                conversations.c.member_id == member.id,
                conversation_members.c.role.in_(ACL_ROLES),
            )
        )
    row = (await conn.execute(stmt)).mappings().first()
    if row is None:
        return None, None
    data = dict(row)
    acl_role = data.pop("_access_role", None)
    if member.role in ADMIN_ROLES:
        role = "admin"
    elif str(data.get("member_id")) == str(member.id):
        role = "owner"
    else:
        role = str(acl_role) if acl_role in ACL_ROLES else None
    return data, role


async def conversation_access(
    member: Member,
    conversation_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    conversations = await reflect_table("conversations")
    try:
        conversation_members = await reflect_table("conversation_members")
    except Exception:
        conversation_members = None
    async with engine.begin() as conn:
        return await access_in_connection(
            conn,
            conversations,
            conversation_members,
            member=member,
            conversation_id=str(conversation_id),
        )


async def require_conversation(
    member: Member,
    conversation_id: str,
    *,
    minimum: str = "viewer",
) -> tuple[dict[str, Any], str]:
    row, role = await conversation_access(member, conversation_id)
    if row is None or not role_allows(role, minimum):
        raise LookupError("Conversation not found")
    return row, str(role)


async def active_org_member(organization_id: str, member_id: str) -> dict[str, Any] | None:
    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(
                    members.c.id,
                    members.c.email,
                    members.c.name,
                    members.c.role,
                    members.c.status,
                ).where(
                    members.c.id == str(member_id),
                    members.c.organization_id == organization_id,
                    members.c.status == "active",
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def list_conversation_members(
    organization_id: str,
    conversation_id: str,
) -> list[dict[str, Any]]:
    acl = await reflect_table("conversation_members")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    acl.c.member_id,
                    acl.c.role,
                    acl.c.granted_by_member_id,
                    acl.c.created_at,
                    acl.c.updated_at,
                    members.c.email,
                    members.c.name,
                    members.c.status,
                )
                .select_from(
                    acl.join(
                        members,
                        and_(
                            members.c.id == acl.c.member_id,
                            members.c.organization_id == acl.c.organization_id,
                        ),
                    )
                )
                .where(
                    acl.c.organization_id == organization_id,
                    acl.c.conversation_id == conversation_id,
                )
                .order_by(acl.c.created_at.asc())
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def upsert_conversation_member(
    *,
    organization_id: str,
    conversation_id: str,
    member_id: str,
    role: str,
    granted_by_member_id: str,
) -> dict[str, Any]:
    if role not in SHAREABLE_ROLES:
        raise ValueError("role must be editor or viewer")
    target = await active_org_member(organization_id, member_id)
    if target is None:
        raise LookupError("Member not found")

    acl = await reflect_table("conversation_members")
    conversations = await reflect_table("conversations")
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        conversation = (
            await conn.execute(
                select(conversations.c.member_id).where(
                    conversations.c.id == conversation_id,
                    conversations.c.organization_id == organization_id,
                )
            )
        ).first()
        if conversation is None:
            raise LookupError("Conversation not found")
        if str(conversation.member_id) == str(member_id):
            raise ValueError("The conversation owner role cannot be changed")
        row = (
            await conn.execute(
                pg_insert(acl)
                .values(
                    organization_id=organization_id,
                    conversation_id=conversation_id,
                    member_id=str(member_id),
                    role=role,
                    granted_by_member_id=str(granted_by_member_id),
                    updated_at=now,
                )
                .on_conflict_do_update(
                    constraint="uq_conversation_members_org_conversation_member",
                    set_={
                        "role": role,
                        "granted_by_member_id": str(granted_by_member_id),
                        "updated_at": now,
                    },
                    where=acl.c.role != "owner",
                )
                .returning(
                    acl.c.member_id,
                    acl.c.role,
                    acl.c.granted_by_member_id,
                    acl.c.created_at,
                    acl.c.updated_at,
                )
            )
        ).mappings().first()
    if row is None:
        raise ValueError("The conversation owner role cannot be changed")
    return {**dict(row), "email": target.get("email"), "name": target.get("name")}


async def remove_conversation_member(
    *,
    organization_id: str,
    conversation_id: str,
    member_id: str,
) -> bool:
    acl = await reflect_table("conversation_members")
    async with engine.begin() as conn:
        result = await conn.execute(
            delete(acl).where(
                acl.c.organization_id == organization_id,
                acl.c.conversation_id == conversation_id,
                acl.c.member_id == str(member_id),
                acl.c.role != "owner",
            )
        )
    return bool(result.rowcount)


async def touch_conversation_member(
    *,
    organization_id: str,
    conversation_id: str,
    member_id: str,
) -> None:
    """Keep the ACL timestamp useful after a collaborator contributes."""

    acl = await reflect_table("conversation_members")
    async with engine.begin() as conn:
        await conn.execute(
            update(acl)
            .where(
                acl.c.organization_id == organization_id,
                acl.c.conversation_id == conversation_id,
                acl.c.member_id == str(member_id),
            )
            .values(updated_at=datetime.now(timezone.utc))
        )
