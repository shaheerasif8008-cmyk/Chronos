"""Member invitations.

Invitations are the governed onboarding path for a tenant: an admin invites an
email + role, and that email becomes a member only when it authenticates against
a pending, non-expired invitation. All writes are tenant-scoped and audited.

When SendGrid is configured, creation delivers a real email. Otherwise it
returns a functional secure invitation URL for the administrator to copy. The
API never claims an email was sent when it was not.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import insert, select, update

from core import audit, notification_delivery
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
        "delivery_status": row.get("delivery_status") or "pending",
        "delivery_channel": row.get("delivery_channel") or "manual_link",
        "delivery_error": row.get("delivery_error"),
        "last_delivery_attempt_at": (
            row.get("last_delivery_attempt_at").isoformat()
            if row.get("last_delivery_attempt_at")
            else None
        ),
        "sent_at": row.get("sent_at").isoformat() if row.get("sent_at") else None,
    }


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _invitation_context(org_id: str) -> dict[str, str]:
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(
                    organizations.c.name,
                    organizations.c.slug,
                    organizations.c.subdomain,
                ).where(organizations.c.id == org_id)
            )
        ).mappings().first()
    if row is None:
        return {"name": "your Chronos workspace", "tenant": ""}
    return {
        "name": str(row.get("name") or "your Chronos workspace"),
        "tenant": str(row.get("subdomain") or row.get("slug") or ""),
    }


def _invitation_url(raw_token: str, *, tenant: str) -> str:
    if settings.is_production and tenant:
        base = f"https://{tenant}.{settings.base_domain}"
    else:
        base = settings.frontend_base_url.rstrip("/")
    return f"{base}/login?{urlencode({'invite': raw_token})}"


async def _record_delivery(
    invitation_id: str,
    *,
    status: str,
    channel: str,
    error: str | None,
) -> dict[str, Any]:
    table = await reflect_table("invitations")
    now = _now()
    values: dict[str, Any] = {
        "delivery_status": status,
        "delivery_channel": channel,
        "delivery_error": error,
        "last_delivery_attempt_at": now,
    }
    if status == "sent":
        values["sent_at"] = now
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                update(table)
                .where(table.c.id == invitation_id)
                .values(**values)
                .returning(table)
            )
        ).mappings().one()
    return dict(row)


async def _deliver_invitation(
    inviter: Member, row: dict[str, Any], raw_token: str
) -> dict[str, Any]:
    context = await _invitation_context(inviter.organization_id)
    invite_url = _invitation_url(raw_token, tenant=context["tenant"])
    status = "manual_required"
    channel = "manual_link"
    error: str | None = "email_provider_not_configured"

    if notification_delivery.email_is_configured():
        try:
            await asyncio.to_thread(
                notification_delivery._provider_send_email,
                to=str(row["email"]),
                subject=f"You're invited to {context['name']} on Chronos",
                body=(
                    f"{inviter.email} invited you to {context['name']} on Chronos "
                    f"as {row['role']}.\n\nAccept the invitation within 7 days:\n"
                    f"{invite_url}\n\nIf you were not expecting this invitation, ignore this email."
                ),
            )
            status = "sent"
            channel = "email"
            error = None
        except (
            notification_delivery.EmailNotConfigured,
            notification_delivery.EmailDeliveryError,
        ):
            # Preserve a usable manual path. Provider details are intentionally
            # not returned or persisted; only this stable failure category is.
            error = "email_provider_failed"

    delivered = await _record_delivery(
        str(row["id"]), status=status, channel=channel, error=error
    )
    await audit.log(
        "invitation_delivery",
        inviter.id,
        "settings.deliver_invitation",
        organization_id=inviter.organization_id,
        resource_type="invitation",
        resource_id=str(row["id"]),
        payload={"status": status, "channel": channel, "email": row["email"]},
    )
    public = _public(delivered)
    if status != "sent":
        # Returned only on the creation response. Listing never exposes it.
        public["invite_url"] = invite_url
    return public


async def create_invitation(inviter: Member, email: str, role: str) -> dict[str, Any]:
    """Create (or refresh) a pending invitation for ``email`` in the inviter's org."""
    email = email.lower().strip()
    table = await reflect_table("invitations")
    raw_token = secrets.token_urlsafe(32)
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
                    # Store only a one-way digest. The raw bearer token exists
                    # long enough to build the email/manual URL and is never
                    # persisted or written to logs.
                    token=_token_digest(raw_token),
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
    return await _deliver_invitation(inviter, dict(row), raw_token)


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


async def resolve_invitation(raw_token: str) -> dict[str, Any] | None:
    """Resolve an opaque invite link into safe login-routing metadata."""

    if not raw_token or len(raw_token) > 512:
        return None
    invitations = await reflect_table("invitations")
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(
                    invitations.c.id,
                    invitations.c.email,
                    invitations.c.role,
                    invitations.c.status,
                    invitations.c.expires_at,
                    organizations.c.name.label("organization_name"),
                    organizations.c.slug,
                    organizations.c.subdomain,
                )
                .select_from(
                    invitations.join(
                        organizations,
                        organizations.c.id == invitations.c.organization_id,
                    )
                )
                .where(
                    invitations.c.token == _token_digest(raw_token),
                    invitations.c.status == "pending",
                )
            )
        ).mappings().first()
        if row is None:
            return None
        expires_at = row.get("expires_at")
        if expires_at is not None and expires_at < _now():
            await conn.execute(
                update(invitations)
                .where(invitations.c.id == row["id"])
                .values(status="expired")
            )
            return None
    return {
        "email": str(row["email"]),
        "role": str(row["role"]),
        "organization_name": str(row.get("organization_name") or "Chronos workspace"),
        "tenant": str(row.get("subdomain") or row.get("slug") or ""),
        "expires_at": row.get("expires_at").isoformat() if row.get("expires_at") else None,
    }


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
                select(invitations)
                .where(
                    invitations.c.organization_id == org_id,
                    invitations.c.email == email,
                    invitations.c.status == "pending",
                )
                .with_for_update()
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
