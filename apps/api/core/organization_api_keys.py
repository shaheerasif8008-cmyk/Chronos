"""Organization API key issuance and authentication.

Plaintext keys exist only in the return value from create/rotate. Persistence
uses an indexed random lookup id plus an HMAC digest derived from the stable
vault encryption key, so a database-only compromise cannot recover keys and
independent JWT-signing rotation does not silently revoke programmatic access.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import insert, select, update

from core.audit_redaction import redact
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from core.redis import redis_client


KEY_MARKER = "chr_live_"
ALLOWED_SCOPES = {"read", "write", "admin"}


def _digest(value: str, *, purpose: str) -> str:
    raw_vault_key = settings.vault_encryption_key.strip()
    try:
        vault_key = bytes.fromhex(raw_vault_key) if raw_vault_key else b"chronos-development-vault-key"
    except ValueError:
        # Production configuration validation rejects malformed vault keys at
        # startup. Keep development diagnostics deterministic without coupling
        # API keys to the independently rotated JWT signing key.
        vault_key = raw_vault_key.encode("utf-8")
    derived_key = hmac.new(
        vault_key,
        b"chronos/organization-api-keys/v1",
        hashlib.sha256,
    ).digest()
    return hmac.new(
        derived_key,
        f"{purpose}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _new_material() -> tuple[str, str, str, str]:
    lookup_id = secrets.token_hex(10)
    secret = secrets.token_urlsafe(32)
    plaintext = f"{KEY_MARKER}{lookup_id}_{secret}"
    prefix = f"{KEY_MARKER}{lookup_id[:8]}…"
    return lookup_id, plaintext, prefix, _digest(plaintext, purpose="org-api-key")


def _parse_lookup_id(token: str) -> str | None:
    if not token.startswith(KEY_MARKER):
        return None
    remainder = token[len(KEY_MARKER) :]
    lookup_id, separator, secret = remainder.partition("_")
    if separator != "_" or len(lookup_id) != 20 or not secret:
        return None
    try:
        int(lookup_id, 16)
    except ValueError:
        return None
    return lookup_id


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "key_prefix": str(row["key_prefix"]),
        "scopes": list(row.get("scopes") or []),
        "rate_limit_per_minute": int(row["rate_limit_per_minute"]),
        "status": str(row["status"]),
        "expires_at": row.get("expires_at"),
        "last_used_at": row.get("last_used_at"),
        "created_by_member_id": str(row["created_by_member_id"]),
        "rotated_from_id": str(row["rotated_from_id"]) if row.get("rotated_from_id") else None,
        "created_at": row.get("created_at"),
        "revoked_at": row.get("revoked_at"),
    }


async def _append_audit(
    conn,
    audit_log,
    actor: Member,
    *,
    action: str,
    resource_id: str,
    payload: dict[str, Any],
    decision: str,
) -> None:
    await conn.execute(
        insert(audit_log).values(
            organization_id=actor.organization_id,
            region=actor.region,
            event_type="credential_change",
            actor_id=actor.id,
            action=action,
            resource_type="organization_api_key",
            resource_id=resource_id,
            payload=redact(payload),
            decision=decision,
        )
    )


def _validated_scopes(scopes: list[str]) -> list[str]:
    normalized = sorted({str(scope).strip().lower() for scope in scopes})
    if not normalized or not set(normalized).issubset(ALLOWED_SCOPES):
        raise ValueError("Scopes must contain read, write, or admin")
    return normalized


async def list_keys(org_id: str) -> list[dict[str, Any]]:
    keys = await reflect_table("organization_api_keys")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(keys)
                .where(keys.c.organization_id == org_id)
                .order_by(keys.c.created_at.desc())
            )
        ).mappings().all()
    return [_public(dict(row)) for row in rows]


async def create_key(
    actor: Member,
    *,
    name: str,
    scopes: list[str],
    rate_limit_per_minute: int,
    expires_at: datetime | None,
    rotated_from_id: str | None = None,
) -> dict[str, Any]:
    normalized_name = name.strip()
    if not normalized_name or len(normalized_name) > 120:
        raise ValueError("API key name must be between 1 and 120 characters")
    normalized_scopes = _validated_scopes(scopes)
    if "admin" in normalized_scopes and actor.role != "owner":
        raise PermissionError("Only an organization owner can issue an admin-scoped key")
    if not 1 <= rate_limit_per_minute <= 6000:
        raise ValueError("Rate limit must be between 1 and 6000 requests per minute")
    now = datetime.now(timezone.utc)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            raise ValueError("Expiry must be in the future")

    lookup_id, plaintext, prefix, secret_hash = _new_material()
    keys = await reflect_table("organization_api_keys")
    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(keys)
                .values(
                    organization_id=actor.organization_id,
                    region=actor.region,
                    name=normalized_name,
                    lookup_id=lookup_id,
                    key_prefix=prefix,
                    secret_hash=secret_hash,
                    scopes=normalized_scopes,
                    rate_limit_per_minute=rate_limit_per_minute,
                    expires_at=expires_at,
                    created_by_member_id=actor.id,
                    rotated_from_id=rotated_from_id,
                )
                .returning(keys)
            )
        ).mappings().one()
        await _append_audit(
            conn,
            audit_log,
            actor,
            action="organization_api_key.created",
            resource_id=str(row["id"]),
            payload={
                "name": normalized_name,
                "key_prefix": prefix,
                "scopes": normalized_scopes,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "rate_limit_per_minute": rate_limit_per_minute,
            },
            decision="created",
        )
    result = _public(dict(row))
    result["plaintext_key"] = plaintext
    return result


async def revoke_key(actor: Member, key_id: str, *, reason: str = "revoked") -> bool:
    keys = await reflect_table("organization_api_keys")
    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                update(keys)
                .where(
                    keys.c.id == key_id,
                    keys.c.organization_id == actor.organization_id,
                    keys.c.status == "active",
                )
                .values(
                    status="revoked",
                    revoked_at=datetime.now(timezone.utc),
                    revoked_by=actor.id,
                    updated_at=datetime.now(timezone.utc),
                )
                .returning(keys.c.id, keys.c.key_prefix)
            )
        ).mappings().first()
        if row is None:
            return False
        await _append_audit(
            conn,
            audit_log,
            actor,
            action=f"organization_api_key.{reason}",
            resource_id=str(row["id"]),
            payload={"key_prefix": str(row["key_prefix"])},
            decision="revoked",
        )
    return True


async def rotate_key(actor: Member, key_id: str) -> dict[str, Any] | None:
    keys = await reflect_table("organization_api_keys")
    audit_log = await reflect_table("audit_log")
    lookup_id, plaintext, prefix, secret_hash = _new_material()
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        current = (
            await conn.execute(
                select(keys).where(
                    keys.c.id == key_id,
                    keys.c.organization_id == actor.organization_id,
                    keys.c.status == "active",
                ).with_for_update()
            )
        ).mappings().first()
        if current is None:
            return None
        current_scopes = list(current.get("scopes") or [])
        if "admin" in current_scopes and actor.role != "owner":
            raise PermissionError("Only an organization owner can rotate an admin-scoped key")
        replacement_row = (
            await conn.execute(
                insert(keys)
                .values(
                    organization_id=actor.organization_id,
                    region=actor.region,
                    name=str(current["name"]),
                    lookup_id=lookup_id,
                    key_prefix=prefix,
                    secret_hash=secret_hash,
                    scopes=current_scopes,
                    rate_limit_per_minute=int(current["rate_limit_per_minute"]),
                    expires_at=current.get("expires_at"),
                    created_by_member_id=actor.id,
                    rotated_from_id=str(current["id"]),
                )
                .returning(keys)
            )
        ).mappings().one()
        # This update shares the insert transaction. A crash, constraint error,
        # or connection loss rolls both changes back, so two valid keys cannot
        # survive a partial rotation.
        revoked = await conn.execute(
            update(keys)
            .where(
                keys.c.id == key_id,
                keys.c.organization_id == actor.organization_id,
                keys.c.status == "active",
            )
            .values(
                status="revoked",
                revoked_at=now,
                revoked_by=actor.id,
                updated_at=now,
            )
        )
        if int(revoked.rowcount or 0) != 1:
            raise RuntimeError("API key rotation lost its predecessor lock")
        await _append_audit(
            conn,
            audit_log,
            actor,
            action="organization_api_key.rotated",
            resource_id=str(replacement_row["id"]),
            payload={
                "replaced_key_id": key_id,
                "key_prefix": prefix,
                "scopes": current_scopes,
            },
            decision="rotated",
        )
    replacement = _public(dict(replacement_row))
    replacement["plaintext_key"] = plaintext
    return replacement


async def authenticate_api_key(token: str, request: Request) -> Member:
    lookup_id = _parse_lookup_id(token)
    if lookup_id is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    keys = await reflect_table("organization_api_keys")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(select(keys).where(keys.c.lookup_id == lookup_id))
        ).mappings().first()
        if row is None or not hmac.compare_digest(
            str(row["secret_hash"]), _digest(token, purpose="org-api-key")
        ):
            raise HTTPException(status_code=401, detail="Invalid API key")
        now = datetime.now(timezone.utc)
        expires_at = row.get("expires_at")
        if row["status"] != "active" or (expires_at is not None and expires_at <= now):
            raise HTTPException(status_code=401, detail="API key is revoked or expired")
        member_row = (
            await conn.execute(
                select(members).where(
                    members.c.id == row["created_by_member_id"],
                    members.c.organization_id == row["organization_id"],
                )
            )
        ).mappings().first()
        if member_row is None or member_row.get("status", "active") != "active":
            raise HTTPException(status_code=403, detail="API key principal is inactive")

    minute_bucket = int(datetime.now(timezone.utc).timestamp() // 60)
    rate_key = f"org-api-key:rate:{row['id']}:{minute_bucket}"
    try:
        count = await redis_client.incr(rate_key)
        if count == 1:
            await redis_client.expire(rate_key, 120)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="API key rate limiter unavailable") from exc
    if int(count) > int(row["rate_limit_per_minute"]):
        raise HTTPException(status_code=429, detail="API key rate limit exceeded")

    client_host = request.client.host if request.client else "unknown"
    async with engine.begin() as conn:
        await conn.execute(
            update(keys)
            .where(keys.c.id == row["id"], keys.c.status == "active")
            .values(
                last_used_at=datetime.now(timezone.utc),
                last_used_ip_hash=_digest(client_host, purpose="api-key-client-ip"),
                updated_at=datetime.now(timezone.utc),
            )
        )
    return Member(
        **dict(member_row),
        auth_type="api_key",
        api_key_id=str(row["id"]),
        api_key_scopes=list(row.get("scopes") or []),
    )
