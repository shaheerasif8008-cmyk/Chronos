"""Member invitations.

Invitations are the governed onboarding path for a tenant: an admin invites an
email + role, and that email becomes a member only when it authenticates against
a pending, non-expired invitation. All writes are tenant-scoped and audited.

Email *delivery* is degraded truthfully (no dispatcher is configured in dev, the
same posture as OTP), so create returns the token for the caller to surface.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import insert, select, update

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member

_INVITE_TTL = timedelta(days=7)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """Invitation view safe to return over the API — never leaks the raw token."""
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "role": row["role"],
        "status": row["status"],
        "invited_by": row.get("invited_by"),
        "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        "expires_at": row.get("expires_at").isoformat() if row.get("expires_at") else None,
        "accepted_at": row.get("accepted_at").isoformat() if row.get("accepted_at") else None,
    }


async def create_invitation(inviter: Member, email: str, role: str) -> dict[str, Any]:
    """Create (or refresh) a pending invitation for ``email`` in the inviter's org."""
    email = email.lower().strip()
    table = await reflect_table("invitations")
    token = secrets.token_urlsafe(32)
    expires_at = _now() + _INVITE_TTL
    async with engine.begin() as conn:
        # Supersede any existing pending invite for the same email/org so there is
        # always at most one live invitation per address.
        await conn.execute(
            update(table)
            .where(
                table.c.organization_id == inviter.organization_id,
                table.c.email == email,
                table.c.status == "pending",
            )
            .values(status="revoked", revoked_at=_now())
        )
        row = (
            await conn.execute(
                insert(table)
                .values(
                    organization_id=inviter.organization_id,
                    region=settings.region,
                    email=email,
                    role=role,
                    token=token,
                    status="pending",
                    invited_by=inviter.id,
                    expires_at=expires_at,
                )
                .returning(table)
            )
        ).mappings().one()
    await audit.log(
        "invitation_created",
        inviter.id,
        "settings.invite_member",
        organization_id=inviter.organization_id,
        resource_type="invitation",
        resource_id=str(row["id"]),
        payload={"email": email, "role": role},
    )
    public = _public(dict(row))
    # The token is returned once, here, for the caller to deliver — never stored
    # in audit and never returned by list_invitations.
    public["token"] = token
    return public


async def list_invitations(org_id: str) -> list[dict[str, Any]]:
    table = await reflect_table("invitations")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(table)
                .where(table.c.organization_id == org_id)
                .order_by(table.c.created_at.desc())
            )
        ).mappings().all()
    return [_public(dict(r)) for r in rows]


async def revoke_invitation(actor: Member, invitation_id: str) -> bool:
    table = await reflect_table("invitations")
    async with engine.begin() as conn:
        result = await conn.execute(
            update(table)
            .where(
                table.c.id == invitation_id,
                table.c.organization_id == actor.organization_id,
                table.c.status == "pending",
            )
            .values(status="revoked", revoked_at=_now())
        )
        revoked = result.rowcount > 0
    if revoked:
        await audit.log(
            "invitation_revoked",
            actor.id,
            "settings.revoke_invitation",
            organization_id=actor.organization_id,
            resource_type="invitation",
            resource_id=invitation_id,
        )
    return revoked


async def accept_pending_invitation(email: str, *, org_id: str, name: str | None = None) -> Member | None:
    """Provision a member from a pending, non-expired invitation, or return None.

    Called from the auth flow when an authenticating email is not yet a member.
    Atomically marks the invitation accepted and inserts the member with the
    invited role.
    """
    email = email.lower().strip()
    invitations = await reflect_table("invitations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        invite = (
            await conn.execute(
                select(invitations).where(
                    invitations.c.organization_id == org_id,
                    invitations.c.email == email,
                    invitations.c.status == "pending",
                )
            )
        ).mappings().first()
        if invite is None:
            return None
        expires_at = invite.get("expires_at")
        if expires_at is not None and expires_at < _now():
            await conn.execute(
                update(invitations).where(invitations.c.id == invite["id"]).values(status="expired")
            )
            return None
        await conn.execute(
            update(invitations)
            .where(invitations.c.id == invite["id"])
            .values(status="accepted", accepted_at=_now())
        )
        row = (
            await conn.execute(
                insert(members)
                .values(
                    organization_id=org_id,
                    region=settings.region,
                    email=email,
                    role=invite["role"],
                    name=name or email.split("@", 1)[0],
                )
                .returning(members)
            )
        ).mappings().one()
    member = Member(**dict(row))
    await audit.log(
        "invitation_accepted",
        member.id,
        "auth.accept_invitation",
        organization_id=org_id,
        resource_type="invitation",
        resource_id=str(invite["id"]),
    )
    return member
