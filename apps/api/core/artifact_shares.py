"""Artifact publish/share: signed-token public links with revocation."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from core.db import engine, reflect_table


async def create_share(artifact_id: str, *, org_id: str, created_by: str | None = None) -> dict[str, Any]:
    """Create (or reactivate) a public share link for an artifact. Returns the share row.

    Idempotent and race-safe: a partial unique index (one active share per
    artifact/org) guarantees at most one active row; a concurrent insert that
    loses the race is caught and the existing active row is returned.
    """
    shares = await reflect_table("artifact_shares")
    existing = await get_share_for_artifact(artifact_id, org_id)
    if existing:
        return existing
    token = secrets.token_urlsafe(24)
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
    shares = await reflect_table("artifact_shares")
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(shares).where(shares.c.token == token, shares.c.status == "active")
        )).mappings().first()
    return dict(row) if row else None


async def get_share_for_artifact(artifact_id: str, org_id: str) -> dict[str, Any] | None:
    shares = await reflect_table("artifact_shares")
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(shares).where(
                shares.c.artifact_id == artifact_id,
                shares.c.organization_id == org_id,
                shares.c.status == "active",
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
