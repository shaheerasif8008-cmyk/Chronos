"""
Credential vault — AES-256-GCM encryption with Redis (fast) + Postgres (backup).

Security contract:
  - Decrypted credential dicts NEVER appear in logs.
  - Only vault_ref is safe to log.
  - vault_entries.encrypted_data must never be returned via any API endpoint.
"""
import json
import secrets
from typing import Any

from sqlalchemy import delete as sa_delete, insert, select

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.exceptions import VaultError
from core.redis import redis_client

_REDIS_TTL = 86_400  # 24 h cache


def _get_cipher():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:
        raise VaultError("cryptography package not installed") from e
    if not settings.vault_encryption_key:
        raise VaultError("VAULT_ENCRYPTION_KEY is not set")
    key_bytes = bytes.fromhex(settings.vault_encryption_key)
    if len(key_bytes) != 32:
        raise VaultError("VAULT_ENCRYPTION_KEY must be exactly 32 bytes (64 hex chars)")
    return AESGCM(key_bytes)


def _encrypt(data: dict[str, Any]) -> str:
    """Return hex-encoded nonce+ciphertext."""
    aesgcm = _get_cipher()
    nonce = secrets.token_bytes(12)
    plaintext = json.dumps(data).encode()
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return (nonce + ciphertext).hex()


def _decrypt(encrypted_hex: str) -> dict[str, Any]:
    aesgcm = _get_cipher()
    raw = bytes.fromhex(encrypted_hex)
    nonce, ciphertext = raw[:12], raw[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext.decode())


async def store(connector_id: str, credentials: dict[str, Any], org_id: str = "default") -> str:
    """Encrypt and persist credentials; return the vault_ref for logging."""
    vault_ref = f"vlt_{secrets.token_hex(16)}"
    encrypted = _encrypt(credentials)

    # Postgres backup first (durable)
    vault_entries = await reflect_table("vault_entries")
    async with engine.begin() as conn:
        await conn.execute(
            insert(vault_entries).values(
                organization_id=org_id,
                vault_ref=vault_ref,
                encrypted_data=encrypted,
            )
        )

    # Redis fast cache
    await redis_client.set(f"vault:data:{vault_ref}", encrypted, ex=_REDIS_TTL)

    # Only vault_ref in audit — never the credential content
    await audit.log("vault_write", connector_id, "vault.store", organization_id=org_id, resource_type="vault_entries", resource_id=vault_ref)
    return vault_ref


async def get(vault_ref: str) -> dict[str, Any]:
    """Retrieve and decrypt credentials.  vault_ref is safe to log; return value is NOT."""
    # Try Redis first
    cached = await redis_client.get(f"vault:data:{vault_ref}")
    if cached:
        raw = cached if isinstance(cached, str) else cached.decode()
        return _decrypt(raw)

    # Fall back to Postgres
    vault_entries = await reflect_table("vault_entries")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(vault_entries.c.encrypted_data).where(vault_entries.c.vault_ref == vault_ref)
            )
        ).scalar_one_or_none()

    if row is None:
        raise VaultError(f"vault_ref not found: {vault_ref}")

    # Re-populate Redis cache
    await redis_client.set(f"vault:data:{vault_ref}", row, ex=_REDIS_TTL)
    return _decrypt(row)


async def update(vault_ref: str, credentials: dict[str, Any], actor_id: str = "system") -> None:
    """Re-encrypt and overwrite credentials for an existing vault_ref.

    Used for in-place token refresh so the vault_ref (and any FK reference to it
    in the connectors table) remains stable.
    """
    from sqlalchemy import update as sa_update

    encrypted = _encrypt(credentials)

    vault_entries = await reflect_table("vault_entries")
    async with engine.begin() as conn:
        # Capture the entry's tenant from the row itself — callers (deep in token
        # refresh) don't carry org context, but the vault row owns it.
        org_id = (
            await conn.execute(
                sa_update(vault_entries)
                .where(vault_entries.c.vault_ref == vault_ref)
                .values(encrypted_data=encrypted)
                .returning(vault_entries.c.organization_id)
            )
        ).scalar_one_or_none()

    # Fail closed: no matching row means an unknown vault_ref. Don't repopulate
    # the Redis cache or emit an audit entry under the process-default org —
    # that would create a cache-only credential and mis-attribute the tenant.
    if org_id is None:
        raise VaultError(f"vault_ref not found: {vault_ref}")

    # Overwrite Redis cache
    await redis_client.set(f"vault:data:{vault_ref}", encrypted, ex=_REDIS_TTL)
    await audit.log("vault_update", actor_id, "vault.update", organization_id=org_id, resource_type="vault_entries", resource_id=vault_ref)


async def delete(vault_ref: str, actor_id: str, org_id: str = "default") -> None:
    """Delete cached and durable encrypted credentials for a disconnected connector."""
    await redis_client.delete(f"vault:data:{vault_ref}")
    vault_entries = await reflect_table("vault_entries")
    async with engine.begin() as conn:
        await conn.execute(
            sa_delete(vault_entries).where(
                vault_entries.c.organization_id == org_id,
                vault_entries.c.vault_ref == vault_ref,
            )
        )
    await audit.log("vault_delete", actor_id, "vault.delete", organization_id=org_id, resource_type="vault_entries", resource_id=vault_ref)
