"""Artifact publish/share: signed-token public links with revocation."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from core.db import engine, reflect_table


async def _expire_due_shares(*, token: str | None = None) -> None:
    shares = await reflect_table("artifact_shares")
    if "expires_at" not in shares.c:
        return
    now = datetime.now(timezone.utc)
    predicates = [
        shares.c.status == "active",
        shares.c.expires_at.is_not(None),
        shares.c.expires_at <= now,
    ]
    if token is not None:
        predicates.append(shares.c.token == token)
    async with engine.begin() as conn:
        await conn.execute(
            update(shares)
            .where(*predicates)
            .values(status="expired", revoked_at=now)
        )


async def create_share(
    artifact_id: str,
    *,
    org_id: str,
    created_by: str | None = None,
    expires_in_hours: int | None = None,
) -> dict[str, Any]:
    """Create (or reactivate) a public share link for an artifact. Returns the share row.

    Idempotent and race-safe: a partial unique index (one active share per
    artifact/org) guarantees at most one active row; a concurrent insert that
    loses the race is caught and the existing active row is returned.
    """
    from core.config import settings

    ttl_hours = (
        settings.artifact_share_ttl_hours
        if expires_in_hours is None
        else expires_in_hours
    )
    if not 1 <= ttl_hours <= settings.artifact_share_ttl_hours:
        raise ValueError(
            f"Public link duration must be between 1 and {settings.artifact_share_ttl_hours} hours"
        )
    shares = await reflect_table("artifact_shares")
    await _expire_due_shares()
    existing = await get_share_for_artifact(artifact_id, org_id)
    if existing:
        return existing
    token = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(
                insert(shares).values(
                    organization_id=org_id,
                    artifact_id=artifact_id,
                    token=token,
                    visibility="public_link",
                    status="active",
                    created_by=created_by,
                    expires_at=expires_at,
                ).returning(shares)
            )).mappings().first()
        return dict(row)
    except IntegrityError:
        # Concurrent caller created the active share first — return theirs.
        existing = await get_share_for_artifact(artifact_id, org_id)
        if existing:
            return existing
        raise


async def get_active_share_by_token(token: str) -> dict[str, Any] | None:
    await _expire_due_shares(token=token)
    shares = await reflect_table("artifact_shares")
    expiry_clause = (
        shares.c.expires_at > datetime.now(timezone.utc)
        if "expires_at" in shares.c
        else True
    )
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(shares).where(
                shares.c.token == token,
                shares.c.status == "active",
                expiry_clause,
            )
        )).mappings().first()
    return dict(row) if row else None


async def get_share_for_artifact(artifact_id: str, org_id: str) -> dict[str, Any] | None:
    await _expire_due_shares()
    shares = await reflect_table("artifact_shares")
    expiry_clause = (
        shares.c.expires_at > datetime.now(timezone.utc)
        if "expires_at" in shares.c
        else True
    )
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(shares).where(
                shares.c.artifact_id == artifact_id,
                shares.c.organization_id == org_id,
                shares.c.status == "active",
                expiry_clause,
            )
        )).mappings().first()
    return dict(row) if row else None


async def revoke_share(artifact_id: str, org_id: str) -> bool:
    shares = await reflect_table("artifact_shares")
    async with engine.begin() as conn:
        res = await conn.execute(
            update(shares)
            .where(
                shares.c.artifact_id == artifact_id,
                shares.c.organization_id == org_id,
                shares.c.status == "active",
            )
            .values(status="revoked", revoked_at=datetime.now(timezone.utc))
        )
    return res.rowcount > 0
