"""Authenticated, tenant-bound command bridge for user-owned desktop devices.

The API is only a control plane.  It never resolves client filesystem paths or
executes bridge commands itself.  A paired device polls a durable queue, verifies
an HMAC-signed command envelope, executes inside its own security-scoped folder
bookmark, and submits a signed result.  Bearer tokens are stored only as hashes;
per-device HMAC keys are encrypted with the deployment vault key and tenant-bound
associated data.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from core import audit
from core.config import settings
from core.db import engine, reflect_table


PAIR_CODE_TTL_SECONDS = 10 * 60
COMMAND_TTL_SECONDS = 5 * 60
LEASE_SECONDS = 30
MAX_COMMAND_BYTES = 512_000
MAX_RESULT_BYTES = 512_000
MAX_COMMAND_ATTEMPTS = 3
PAIR_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
COMMAND_TYPES = frozenset({"list_files", "read_file", "exec", "open_app", "revoke_grant", "notify"})
RESULT_STATUSES = frozenset({"succeeded", "failed"})
_DEVICE_TOKEN_PREFIX = "chd_"
_ENCRYPTED_SECRET_PREFIX = "enc:v1:"


class DesktopBridgeError(Exception):
    """Stable, secret-free bridge error suitable for an HTTP response."""

    def __init__(self, code: str, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return _aware(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _token_hash(token: str) -> str:
    return _sha256(token.encode("utf-8"))


def _pair_code_hash(code: str) -> str:
    normalized = "".join(ch for ch in code.upper() if ch.isalnum())
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        f"desktop-pair:v1:{normalized}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _secret_aad(organization_id: str, device_id: str) -> bytes:
    return f"desktop-command-secret:v1\n{organization_id}\n{device_id}".encode("utf-8")


def _vault_cipher():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - pinned production dependency
        raise DesktopBridgeError(
            "encryption_unavailable",
            "Desktop-device secret encryption is unavailable",
            status_code=503,
        ) from exc
    try:
        key = bytes.fromhex(settings.vault_encryption_key.strip())
    except ValueError as exc:
        raise DesktopBridgeError(
            "invalid_vault_key", "Desktop-device bridge encryption is not configured", status_code=503
        ) from exc
    if len(key) != 32:
        raise DesktopBridgeError(
            "invalid_vault_key", "Desktop-device bridge encryption is not configured", status_code=503
        )
    return AESGCM(key)


def protect_command_secret(secret: bytes, *, organization_id: str, device_id: str) -> str:
    nonce = secrets.token_bytes(12)
    encrypted = _vault_cipher().encrypt(nonce, secret, _secret_aad(organization_id, device_id))
    return _ENCRYPTED_SECRET_PREFIX + base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")


def reveal_command_secret(value: str, *, organization_id: str, device_id: str) -> bytes:
    if not value.startswith(_ENCRYPTED_SECRET_PREFIX):
        raise DesktopBridgeError("invalid_device_secret", "Stored device secret is invalid", status_code=503)
    try:
        raw = base64.urlsafe_b64decode(value[len(_ENCRYPTED_SECRET_PREFIX) :].encode("ascii"))
        secret = _vault_cipher().decrypt(raw[:12], raw[12:], _secret_aad(organization_id, device_id))
    except DesktopBridgeError:
        raise
    except Exception as exc:
        raise DesktopBridgeError(
            "invalid_device_secret", "Stored device secret could not be decrypted", status_code=503
        ) from exc
    if len(secret) != 32:
        raise DesktopBridgeError("invalid_device_secret", "Stored device secret is invalid", status_code=503)
    return secret


def command_signing_message(
    *,
    command_id: str,
    device_id: str,
    nonce: str,
    command_type: str,
    expires_at: str,
    payload: bytes,
) -> bytes:
    """Canonical Swift/server command HMAC input.  Do not change without versioning."""

    return (
        f"command:v1\n{command_id}\n{device_id}\n{nonce}\n{command_type}\n"
        f"{expires_at}\n{_sha256(payload)}"
    ).encode("utf-8")


def result_signing_message(
    *,
    command_id: str,
    device_id: str,
    nonce: str,
    status: str,
    error_code: str | None,
    result: bytes,
) -> bytes:
    """Canonical Swift/server result HMAC input.  Do not change without versioning."""

    return (
        f"result:v1\n{command_id}\n{device_id}\n{nonce}\n{status}\n"
        f"{error_code or ''}\n{_sha256(result)}"
    ).encode("utf-8")


def _sign(secret: bytes, message: bytes) -> str:
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _decode_b64(value: str, *, maximum: int, code: str) -> bytes:
    if len(value) > ((maximum + 2) // 3) * 4 + 4:
        raise DesktopBridgeError(code, f"Payload exceeds {maximum} bytes", status_code=413)
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise DesktopBridgeError(code, "Payload must be valid base64") from exc
    if len(raw) > maximum:
        raise DesktopBridgeError(code, f"Payload exceeds {maximum} bytes", status_code=413)
    return raw


def _json_bytes(value: dict[str, Any]) -> bytes:
    try:
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DesktopBridgeError("invalid_command_payload", "Command payload must be JSON serializable") from exc
    if len(raw) > MAX_COMMAND_BYTES:
        raise DesktopBridgeError(
            "command_too_large", f"Command payload exceeds {MAX_COMMAND_BYTES} bytes", status_code=413
        )
    return raw


def _bounded_capabilities(value: dict[str, Any] | None) -> dict[str, Any]:
    capabilities = dict(value or {})
    try:
        raw = json.dumps(capabilities, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DesktopBridgeError(
            "invalid_capabilities", "Device capabilities must be JSON serializable"
        ) from exc
    if len(raw) > 16_384 or len(capabilities) > 100:
        raise DesktopBridgeError(
            "invalid_capabilities", "Device capabilities exceed the allowed size"
        )
    return capabilities


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _require_uuid(value: str, *, code: str, detail: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise DesktopBridgeError(code, detail) from exc


def _public_device(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "organization_id": str(row["organization_id"]),
        "member_id": str(row["member_id"]),
        "name": str(row["name"]),
        "platform": str(row["platform"]),
        "client_version": row.get("client_version"),
        "capabilities": dict(row.get("capabilities") or {}),
        "status": str(row["status"]),
        "last_seen_at": _iso(row["last_seen_at"]) if row.get("last_seen_at") else None,
        "created_at": _iso(row["created_at"]) if row.get("created_at") else None,
        "updated_at": _iso(row["updated_at"]) if row.get("updated_at") else None,
        "revoked_at": _iso(row["revoked_at"]) if row.get("revoked_at") else None,
    }


def _public_grant(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "organization_id": str(row["organization_id"]),
        "member_id": str(row["member_id"]),
        "device_id": str(row["device_id"]),
        "client_grant_id": str(row["client_grant_id"]),
        "display_name": str(row.get("folder_display_name") or "Folder"),
        "purpose": row.get("purpose"),
        "task_id": row.get("task_id"),
        "status": str(row["status"]),
        "created_at": _iso(row["created_at"]) if row.get("created_at") else None,
        "updated_at": _iso(row["updated_at"]) if row.get("updated_at") else None,
        "revoked_at": _iso(row["revoked_at"]) if row.get("revoked_at") else None,
    }


class DesktopBridgeStore(Protocol):
    async def create_pair_code(self, values: dict[str, Any]) -> None: ...
    async def consume_pair_code(
        self, code_hash: str, now: datetime, device: dict[str, Any], command_secret: bytes
    ) -> dict[str, Any]: ...
    async def device_by_token_hash(self, token_hash: str) -> dict[str, Any] | None: ...
    async def touch_device(self, device_id: str, values: dict[str, Any]) -> dict[str, Any]: ...
    async def list_devices(self, organization_id: str, member_id: str | None) -> list[dict[str, Any]]: ...
    async def get_device(self, device_id: str, organization_id: str, member_id: str | None) -> dict[str, Any] | None: ...
    async def revoke_device(self, device_id: str, organization_id: str, member_id: str | None, now: datetime) -> dict[str, Any] | None: ...
    async def register_grant(self, values: dict[str, Any]) -> dict[str, Any]: ...
    async def list_grants(self, organization_id: str, device_id: str, member_id: str | None) -> list[dict[str, Any]]: ...
    async def get_grant(self, grant_id: str, organization_id: str, member_id: str) -> dict[str, Any] | None: ...
    async def revoke_grant(self, device_id: str, client_grant_id: str, now: datetime) -> dict[str, Any] | None: ...
    async def enqueue_command(self, values: dict[str, Any]) -> dict[str, Any]: ...
    async def cancel_task_commands(
        self, organization_id: str, task_ids: list[str], now: datetime
    ) -> dict[str, int]: ...
    async def lease_commands(self, device_id: str, now: datetime, lease_until: datetime, limit: int) -> list[dict[str, Any]]: ...
    async def get_command(self, command_id: str, device_id: str | None = None) -> dict[str, Any] | None: ...
    async def complete_command(
        self, command_id: str, device_id: str, now: datetime, values: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]: ...
    async def device_health_counts(self, fresh_after: datetime) -> tuple[int, int]: ...


class SQLDesktopBridgeStore:
    """PostgreSQL-backed durable bridge store."""

    async def create_pair_code(self, values: dict[str, Any]) -> None:
        table = await reflect_table("desktop_pair_codes")
        async with engine.begin() as conn:
            await conn.execute(
                delete(table).where(
                    table.c.expires_at
                    < _aware(values["created_at"]) - timedelta(days=1)
                )
            )
            await conn.execute(insert(table).values(**values))

    async def consume_pair_code(
        self, code_hash: str, now: datetime, device: dict[str, Any], command_secret: bytes
    ) -> dict[str, Any]:
        codes = await reflect_table("desktop_pair_codes")
        devices = await reflect_table("desktop_devices")
        members = await reflect_table("members")
        async with engine.begin() as conn:
            row = (
                await conn.execute(select(codes).where(codes.c.code_hash == code_hash).with_for_update())
            ).mappings().first()
            if row is None or row.get("consumed_at") is not None:
                raise DesktopBridgeError("invalid_pair_code", "Pairing code is invalid or already used", status_code=404)
            if _aware(row["expires_at"]) <= now:
                raise DesktopBridgeError("expired_pair_code", "Pairing code has expired", status_code=410)
            active_member = (
                await conn.execute(
                    select(members.c.id).where(
                        members.c.id == row["member_id"],
                        members.c.organization_id == row["organization_id"],
                        members.c.status == "active",
                    )
                )
            ).first()
            if active_member is None:
                raise DesktopBridgeError(
                    "invalid_pair_code", "Pairing code is invalid or already used", status_code=404
                )
            bound = dict(device)
            bound["organization_id"] = str(row["organization_id"])
            bound["member_id"] = str(row["member_id"])
            bound["encrypted_command_secret"] = protect_command_secret(
                command_secret,
                organization_id=bound["organization_id"],
                device_id=str(bound["id"]),
            )
            await conn.execute(insert(devices).values(**bound))
            await conn.execute(
                update(codes).where(codes.c.id == row["id"], codes.c.consumed_at.is_(None)).values(consumed_at=now)
            )
        return bound

    async def device_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        table = await reflect_table("desktop_devices")
        members = await reflect_table("members")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(table)
                    .join(
                        members,
                        and_(
                            members.c.id == table.c.member_id,
                            members.c.organization_id == table.c.organization_id,
                            members.c.status == "active",
                        ),
                    )
                    .where(table.c.token_hash == token_hash)
                )
            ).mappings().first()
        return dict(row) if row else None

    async def touch_device(self, device_id: str, values: dict[str, Any]) -> dict[str, Any]:
        table = await reflect_table("desktop_devices")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(table)
                    .where(table.c.id == device_id, table.c.status == "active")
                    .values(**values)
                    .returning(*table.c)
                )
            ).mappings().first()
        if not row:
            raise DesktopBridgeError("device_revoked", "Device is not active", status_code=401)
        return dict(row)

    async def list_devices(self, organization_id: str, member_id: str | None) -> list[dict[str, Any]]:
        table = await reflect_table("desktop_devices")
        members = await reflect_table("members")
        clauses = [table.c.organization_id == organization_id]
        if member_id is not None:
            clauses.append(table.c.member_id == member_id)
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(table)
                    .join(
                        members,
                        and_(
                            members.c.id == table.c.member_id,
                            members.c.organization_id == table.c.organization_id,
                            members.c.status == "active",
                        ),
                    )
                    .where(*clauses)
                    .order_by(table.c.updated_at.desc())
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def get_device(
        self, device_id: str, organization_id: str, member_id: str | None
    ) -> dict[str, Any] | None:
        table = await reflect_table("desktop_devices")
        members = await reflect_table("members")
        clauses = [table.c.id == device_id, table.c.organization_id == organization_id]
        if member_id is not None:
            clauses.append(table.c.member_id == member_id)
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(table)
                    .join(
                        members,
                        and_(
                            members.c.id == table.c.member_id,
                            members.c.organization_id == table.c.organization_id,
                            members.c.status == "active",
                        ),
                    )
                    .where(*clauses)
                )
            ).mappings().first()
        return dict(row) if row else None

    async def revoke_device(
        self, device_id: str, organization_id: str, member_id: str | None, now: datetime
    ) -> dict[str, Any] | None:
        devices = await reflect_table("desktop_devices")
        commands = await reflect_table("desktop_commands")
        grants = await reflect_table("local_computer_grants")
        clauses = [devices.c.id == device_id, devices.c.organization_id == organization_id]
        if member_id is not None:
            clauses.append(devices.c.member_id == member_id)
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(devices)
                    .where(*clauses)
                    .values(status="revoked", revoked_at=now, updated_at=now)
                    .returning(*devices.c)
                )
            ).mappings().first()
            if row:
                await conn.execute(
                    update(commands)
                    .where(commands.c.device_id == device_id, commands.c.status.in_(["queued", "leased"]))
                    .values(status="cancelled", updated_at=now)
                )
                await conn.execute(
                    update(grants)
                    .where(grants.c.device_id == device_id, grants.c.status == "active")
                    .values(status="revoked", revoked_at=now, updated_at=now)
                )
        return dict(row) if row else None

    async def register_grant(self, values: dict[str, Any]) -> dict[str, Any]:
        table = await reflect_table("local_computer_grants")
        statement = (
            pg_insert(table)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[table.c.device_id, table.c.client_grant_id],
                index_where=(
                    table.c.device_id.is_not(None)
                    & table.c.client_grant_id.is_not(None)
                ),
                set_={
                    "folder_display_name": values["folder_display_name"],
                    "purpose": values.get("purpose"),
                    "updated_at": values["updated_at"],
                },
                where=table.c.status == "active",
            )
            .returning(*table.c)
        )
        async with engine.begin() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        if row is None:
            raise DesktopBridgeError(
                "grant_revoked",
                "This folder grant was revoked; authorize it again with a new client grant id",
                status_code=409,
            )
        return dict(row)

    async def list_grants(
        self, organization_id: str, device_id: str, member_id: str | None
    ) -> list[dict[str, Any]]:
        table = await reflect_table("local_computer_grants")
        clauses = [table.c.organization_id == organization_id, table.c.device_id == device_id]
        if member_id is not None:
            clauses.append(table.c.member_id == member_id)
        async with engine.begin() as conn:
            rows = (
                await conn.execute(select(table).where(*clauses).order_by(table.c.updated_at.desc()))
            ).mappings().all()
        return [dict(row) for row in rows]

    async def get_grant(self, grant_id: str, organization_id: str, member_id: str) -> dict[str, Any] | None:
        table = await reflect_table("local_computer_grants")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(table).where(
                        table.c.id == grant_id,
                        table.c.organization_id == organization_id,
                        table.c.member_id == member_id,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    async def revoke_grant(
        self, device_id: str, client_grant_id: str, now: datetime
    ) -> dict[str, Any] | None:
        table = await reflect_table("local_computer_grants")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(table)
                    .where(table.c.device_id == device_id, table.c.client_grant_id == client_grant_id)
                    .values(status="revoked", revoked_at=now, updated_at=now)
                    .returning(*table.c)
                )
            ).mappings().first()
        return dict(row) if row else None

    async def enqueue_command(self, values: dict[str, Any]) -> dict[str, Any]:
        table = await reflect_table("desktop_commands")
        async with engine.begin() as conn:
            # Results may contain local file/command output. They are not audit
            # records and are opportunistically purged after a short recovery
            # window so sensitive client data does not accumulate indefinitely.
            await conn.execute(
                delete(table).where(
                    table.c.organization_id == values["organization_id"],
                    table.c.expires_at
                    < _aware(values["created_at"]) - timedelta(days=7),
                )
            )
            row = (await conn.execute(insert(table).values(**values).returning(*table.c))).mappings().one()
        return dict(row)

    async def cancel_task_commands(
        self, organization_id: str, task_ids: list[str], now: datetime
    ) -> dict[str, int]:
        commands = await reflect_table("desktop_commands")
        grants = await reflect_table("local_computer_grants")
        async with engine.begin() as conn:
            command_result = await conn.execute(
                update(commands)
                .where(
                    commands.c.organization_id == organization_id,
                    commands.c.task_id.in_(task_ids),
                    commands.c.status.in_(["queued", "leased"]),
                )
                .values(
                    status="cancelled",
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            grant_result = await conn.execute(
                update(grants)
                .where(
                    grants.c.organization_id == organization_id,
                    grants.c.task_id.in_(task_ids),
                    grants.c.status == "active",
                )
                .values(status="revoked", revoked_at=now, updated_at=now)
            )
        return {
            "commands": int(command_result.rowcount or 0),
            "grants": int(grant_result.rowcount or 0),
        }

    async def lease_commands(
        self, device_id: str, now: datetime, lease_until: datetime, limit: int
    ) -> list[dict[str, Any]]:
        table = await reflect_table("desktop_commands")
        async with engine.begin() as conn:
            await conn.execute(
                update(table)
                .where(
                    table.c.device_id == device_id,
                    table.c.status.in_(["queued", "leased"]),
                    table.c.expires_at <= now,
                )
                .values(status="expired", updated_at=now)
            )
            await conn.execute(
                update(table)
                .where(
                    table.c.device_id == device_id,
                    table.c.status == "leased",
                    table.c.lease_expires_at <= now,
                    table.c.expires_at > now,
                    table.c.attempts >= table.c.max_attempts,
                )
                .values(status="expired", updated_at=now)
            )
            await conn.execute(
                update(table)
                .where(
                    table.c.device_id == device_id,
                    table.c.status == "leased",
                    table.c.lease_expires_at <= now,
                    table.c.expires_at > now,
                    table.c.attempts < table.c.max_attempts,
                )
                .values(status="queued", leased_at=None, lease_expires_at=None, updated_at=now)
            )
            candidates = (
                await conn.execute(
                    select(table)
                    .where(
                        table.c.device_id == device_id,
                        table.c.status == "queued",
                        table.c.available_at <= now,
                        table.c.expires_at > now,
                    )
                    .order_by(table.c.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).mappings().all()
            leased: list[dict[str, Any]] = []
            for candidate in candidates:
                row = (
                    await conn.execute(
                        update(table)
                        .where(table.c.id == candidate["id"], table.c.status == "queued")
                        .values(
                            status="leased",
                            attempts=table.c.attempts + 1,
                            leased_at=now,
                            lease_expires_at=lease_until,
                            updated_at=now,
                        )
                        .returning(*table.c)
                    )
                ).mappings().first()
                if row:
                    leased.append(dict(row))
        return leased

    async def get_command(self, command_id: str, device_id: str | None = None) -> dict[str, Any] | None:
        table = await reflect_table("desktop_commands")
        clauses = [table.c.id == command_id]
        if device_id is not None:
            clauses.append(table.c.device_id == device_id)
        async with engine.begin() as conn:
            row = (await conn.execute(select(table).where(*clauses))).mappings().first()
        return dict(row) if row else None

    async def complete_command(
        self, command_id: str, device_id: str, now: datetime, values: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        table = await reflect_table("desktop_commands")
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        select(table).where(table.c.id == command_id, table.c.device_id == device_id).with_for_update()
                    )
                ).mappings().first()
                if row is None:
                    raise DesktopBridgeError("command_not_found", "Command not found", status_code=404)
                current = dict(row)
                if current["status"] in RESULT_STATUSES:
                    same = all(
                        str(current.get(key) or "") == str(values.get(key) or "")
                        for key in ("result_nonce", "result_status", "result_error_code", "result_sha256")
                    )
                    if same:
                        return "idempotent", current
                    raise DesktopBridgeError("result_conflict", "Command already has a different result", status_code=409)
                duplicate = (
                    await conn.execute(
                        select(table.c.id).where(
                            table.c.device_id == device_id,
                            table.c.result_nonce == values["result_nonce"],
                            table.c.id != command_id,
                        )
                    )
                ).first()
                if duplicate:
                    raise DesktopBridgeError("result_nonce_reused", "Result nonce was already used", status_code=409)
                if current["status"] != "leased":
                    raise DesktopBridgeError("command_not_leased", "Command does not have an active lease", status_code=409)
                if _aware(current["expires_at"]) <= now:
                    await conn.execute(update(table).where(table.c.id == command_id).values(status="expired", updated_at=now))
                    raise DesktopBridgeError("command_expired", "Command has expired", status_code=410)
                updated = (
                    await conn.execute(
                        update(table)
                        .where(table.c.id == command_id)
                        .values(
                            status=values["result_status"],
                            result_nonce=values["result_nonce"],
                            result_status=values["result_status"],
                            result_error_code=values.get("result_error_code"),
                            result_payload=values["result_payload"],
                            result_sha256=values["result_sha256"],
                            completed_at=now,
                            updated_at=now,
                        )
                        .returning(*table.c)
                    )
                ).mappings().one()
        except IntegrityError as exc:
            # Concurrent submissions on different commands can race the
            # pre-check. The database uniqueness constraint remains the final
            # replay boundary; translate it to the same stable API conflict.
            raise DesktopBridgeError(
                "result_nonce_reused", "Result nonce was already used", status_code=409
            ) from exc
        return "accepted", dict(updated)

    async def device_health_counts(self, fresh_after: datetime) -> tuple[int, int]:
        from sqlalchemy import func

        table = await reflect_table("desktop_devices")
        members = await reflect_table("members")
        active_members = table.join(
            members,
            and_(
                members.c.id == table.c.member_id,
                members.c.organization_id == table.c.organization_id,
                members.c.status == "active",
            ),
        )
        async with engine.begin() as conn:
            active = (
                await conn.execute(
                    select(func.count()).select_from(active_members).where(table.c.status == "active")
                )
            ).scalar_one()
            fresh = (
                await conn.execute(
                    select(func.count()).select_from(active_members).where(
                        table.c.status == "active", table.c.last_seen_at >= fresh_after
                    )
                )
            ).scalar_one()
        return int(active), int(fresh)


class MemoryDesktopBridgeStore:
    """Explicit test store with the same atomic semantics as PostgreSQL."""

    def __init__(self) -> None:
        self.pair_codes: dict[str, dict[str, Any]] = {}
        self.devices: dict[str, dict[str, Any]] = {}
        self.grants: dict[str, dict[str, Any]] = {}
        self.commands: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def create_pair_code(self, values: dict[str, Any]) -> None:
        async with self._lock:
            self.pair_codes[values["code_hash"]] = dict(values)

    async def consume_pair_code(
        self, code_hash: str, now: datetime, device: dict[str, Any], command_secret: bytes
    ) -> dict[str, Any]:
        async with self._lock:
            code = self.pair_codes.get(code_hash)
            if not code or code.get("consumed_at") is not None:
                raise DesktopBridgeError("invalid_pair_code", "Pairing code is invalid or already used", status_code=404)
            if _aware(code["expires_at"]) <= now:
                raise DesktopBridgeError("expired_pair_code", "Pairing code has expired", status_code=410)
            code["consumed_at"] = now
            bound = {**device, "organization_id": code["organization_id"], "member_id": code["member_id"]}
            bound["encrypted_command_secret"] = protect_command_secret(
                command_secret,
                organization_id=str(bound["organization_id"]),
                device_id=str(bound["id"]),
            )
            self.devices[str(bound["id"])] = dict(bound)
            return dict(bound)

    async def device_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        async with self._lock:
            return next((dict(row) for row in self.devices.values() if row["token_hash"] == token_hash), None)

    async def touch_device(self, device_id: str, values: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            row = self.devices.get(device_id)
            if not row or row["status"] != "active":
                raise DesktopBridgeError("device_revoked", "Device is not active", status_code=401)
            row.update(values)
            return dict(row)

    async def list_devices(self, organization_id: str, member_id: str | None) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                dict(row)
                for row in self.devices.values()
                if row["organization_id"] == organization_id
                and (member_id is None or row["member_id"] == member_id)
            ]

    async def get_device(
        self, device_id: str, organization_id: str, member_id: str | None
    ) -> dict[str, Any] | None:
        async with self._lock:
            row = self.devices.get(device_id)
            if not row or row["organization_id"] != organization_id:
                return None
            if member_id is not None and row["member_id"] != member_id:
                return None
            return dict(row)

    async def revoke_device(
        self, device_id: str, organization_id: str, member_id: str | None, now: datetime
    ) -> dict[str, Any] | None:
        async with self._lock:
            row = self.devices.get(device_id)
            if not row or row["organization_id"] != organization_id:
                return None
            if member_id is not None and row["member_id"] != member_id:
                return None
            row.update(status="revoked", revoked_at=now, updated_at=now)
            for command in self.commands.values():
                if command["device_id"] == device_id and command["status"] in {"queued", "leased"}:
                    command.update(status="cancelled", updated_at=now)
            for grant in self.grants.values():
                if grant["device_id"] == device_id and grant["status"] == "active":
                    grant.update(status="revoked", revoked_at=now, updated_at=now)
            return dict(row)

    async def register_grant(self, values: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            existing = next(
                (
                    row
                    for row in self.grants.values()
                    if row["device_id"] == values["device_id"]
                    and row["client_grant_id"] == values["client_grant_id"]
                ),
                None,
            )
            if existing:
                if existing.get("status") != "active":
                    raise DesktopBridgeError(
                        "grant_revoked",
                        "This folder grant was revoked; authorize it again with a new client grant id",
                        status_code=409,
                    )
                existing.update(
                    folder_display_name=values["folder_display_name"],
                    purpose=values.get("purpose"),
                    updated_at=values["updated_at"],
                )
                return dict(existing)
            self.grants[str(values["id"])] = dict(values)
            return dict(values)

    async def list_grants(
        self, organization_id: str, device_id: str, member_id: str | None
    ) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                dict(row)
                for row in self.grants.values()
                if row["organization_id"] == organization_id
                and row["device_id"] == device_id
                and (member_id is None or row["member_id"] == member_id)
            ]

    async def get_grant(self, grant_id: str, organization_id: str, member_id: str) -> dict[str, Any] | None:
        async with self._lock:
            row = self.grants.get(grant_id)
            if not row or row["organization_id"] != organization_id or row["member_id"] != member_id:
                return None
            return dict(row)

    async def revoke_grant(
        self, device_id: str, client_grant_id: str, now: datetime
    ) -> dict[str, Any] | None:
        async with self._lock:
            row = next(
                (
                    row
                    for row in self.grants.values()
                    if row["device_id"] == device_id and row["client_grant_id"] == client_grant_id
                ),
                None,
            )
            if not row:
                return None
            row.update(status="revoked", revoked_at=now, updated_at=now)
            return dict(row)

    async def enqueue_command(self, values: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self.commands[str(values["id"])] = dict(values)
            return dict(values)

    async def cancel_task_commands(
        self, organization_id: str, task_ids: list[str], now: datetime
    ) -> dict[str, int]:
        scoped = set(task_ids)
        commands = 0
        grants = 0
        async with self._lock:
            for row in self.commands.values():
                if (
                    row.get("organization_id") == organization_id
                    and str(row.get("task_id") or "") in scoped
                    and row.get("status") in {"queued", "leased"}
                ):
                    row.update(status="cancelled", lease_expires_at=None, updated_at=now)
                    commands += 1
            for row in self.grants.values():
                if (
                    row.get("organization_id") == organization_id
                    and str(row.get("task_id") or "") in scoped
                    and row.get("status") == "active"
                ):
                    row.update(status="revoked", revoked_at=now, updated_at=now)
                    grants += 1
        return {"commands": commands, "grants": grants}

    async def lease_commands(
        self, device_id: str, now: datetime, lease_until: datetime, limit: int
    ) -> list[dict[str, Any]]:
        async with self._lock:
            for row in self.commands.values():
                if row["device_id"] != device_id or row["status"] not in {"queued", "leased"}:
                    continue
                if _aware(row["expires_at"]) <= now:
                    row.update(status="expired", updated_at=now)
                elif row["status"] == "leased" and _aware(row["lease_expires_at"]) <= now:
                    if row["attempts"] >= row["max_attempts"]:
                        row.update(status="expired", updated_at=now)
                    else:
                        row.update(status="queued", leased_at=None, lease_expires_at=None, updated_at=now)
            candidates = sorted(
                (
                    row
                    for row in self.commands.values()
                    if row["device_id"] == device_id
                    and row["status"] == "queued"
                    and _aware(row["available_at"]) <= now < _aware(row["expires_at"])
                ),
                key=lambda row: row["created_at"],
            )[:limit]
            leased: list[dict[str, Any]] = []
            for row in candidates:
                row.update(
                    status="leased",
                    attempts=row["attempts"] + 1,
                    leased_at=now,
                    lease_expires_at=lease_until,
                    updated_at=now,
                )
                leased.append(dict(row))
            return leased

    async def get_command(self, command_id: str, device_id: str | None = None) -> dict[str, Any] | None:
        async with self._lock:
            row = self.commands.get(command_id)
            if not row or (device_id is not None and row["device_id"] != device_id):
                return None
            return dict(row)

    async def complete_command(
        self, command_id: str, device_id: str, now: datetime, values: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        async with self._lock:
            row = self.commands.get(command_id)
            if not row or row["device_id"] != device_id:
                raise DesktopBridgeError("command_not_found", "Command not found", status_code=404)
            if row["status"] in RESULT_STATUSES:
                same = all(
                    str(row.get(key) or "") == str(values.get(key) or "")
                    for key in ("result_nonce", "result_status", "result_error_code", "result_sha256")
                )
                if same:
                    return "idempotent", dict(row)
                raise DesktopBridgeError("result_conflict", "Command already has a different result", status_code=409)
            if any(
                other["device_id"] == device_id
                and other.get("result_nonce") == values["result_nonce"]
                and other["id"] != command_id
                for other in self.commands.values()
            ):
                raise DesktopBridgeError("result_nonce_reused", "Result nonce was already used", status_code=409)
            if row["status"] != "leased":
                raise DesktopBridgeError("command_not_leased", "Command does not have an active lease", status_code=409)
            if _aware(row["expires_at"]) <= now:
                row.update(status="expired", updated_at=now)
                raise DesktopBridgeError("command_expired", "Command has expired", status_code=410)
            row.update(
                status=values["result_status"],
                result_nonce=values["result_nonce"],
                result_status=values["result_status"],
                result_error_code=values.get("result_error_code"),
                result_payload=values["result_payload"],
                result_sha256=values["result_sha256"],
                completed_at=now,
                updated_at=now,
            )
            return "accepted", dict(row)

    async def device_health_counts(self, fresh_after: datetime) -> tuple[int, int]:
        async with self._lock:
            active = [row for row in self.devices.values() if row["status"] == "active"]
            fresh = [
                row
                for row in active
                if row.get("last_seen_at") and _aware(row["last_seen_at"]) >= fresh_after
            ]
            return len(active), len(fresh)


class DesktopBridgeService:
    def __init__(self, store: DesktopBridgeStore | None = None) -> None:
        self.store: DesktopBridgeStore = store or SQLDesktopBridgeStore()

    async def _audit(self, event: str, actor_id: str, organization_id: str, resource_id: str, **metadata: Any) -> None:
        try:
            await audit.log(
                "activity",
                actor_id,
                event,
                organization_id=organization_id,
                resource_type="desktop_devices",
                resource_id=resource_id,
                payload={"type": event, **metadata},
            )
        except Exception:
            pass

    async def create_pair_code(self, *, organization_id: str, member_id: str) -> dict[str, Any]:
        raw = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(12))
        code = "-".join(raw[index : index + 4] for index in range(0, 12, 4))
        created = _now()
        expires = created + timedelta(seconds=PAIR_CODE_TTL_SECONDS)
        pair_id = str(uuid.uuid4())
        await self.store.create_pair_code(
            {
                "id": pair_id,
                "organization_id": organization_id,
                "member_id": member_id,
                "code_hash": _pair_code_hash(code),
                "expires_at": expires,
                "consumed_at": None,
                "created_at": created,
            }
        )
        await self._audit("desktop_pair_code_created", member_id, organization_id, pair_id)
        return {"pair_code": code, "expires_at": _iso(expires)}

    async def pair_device(
        self,
        *,
        pair_code: str,
        name: str,
        platform: str,
        client_version: str | None,
        capabilities: dict[str, Any] | None,
    ) -> dict[str, Any]:
        name = name.strip()
        platform = platform.strip().lower()
        if not name or len(name) > 120 or _has_control_characters(name):
            raise DesktopBridgeError("invalid_device_name", "Device name must be 1-120 characters")
        if platform not in {"macos", "windows", "linux"}:
            raise DesktopBridgeError("invalid_platform", "Device platform is not supported")
        token = _DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(48)
        secret = secrets.token_bytes(32)
        device_id = str(uuid.uuid4())
        now = _now()
        device = {
            "id": device_id,
            "name": name,
            "platform": platform,
            "client_version": (client_version or "").strip()[:80] or None,
            "capabilities": _bounded_capabilities(capabilities),
            "status": "active",
            "token_hash": _token_hash(token),
            "last_seen_at": now,
            "created_at": now,
            "updated_at": now,
            "revoked_at": None,
        }
        # The store resolves and locks the one-time pair code, then encrypts the
        # secret with the resolved tenant as AAD before inserting the device.
        bound = await self.store.consume_pair_code(
            _pair_code_hash(pair_code), now, device, secret
        )
        await self._audit(
            "desktop_device_paired",
            str(bound["member_id"]),
            str(bound["organization_id"]),
            device_id,
            platform=platform,
        )
        return {
            "device_id": device_id,
            "device_token": token,
            "command_secret_b64": base64.b64encode(secret).decode("ascii"),
        }

    async def authenticate(self, token: str, *, expected_device_id: str | None = None) -> dict[str, Any]:
        if not token.startswith(_DEVICE_TOKEN_PREFIX) or len(token) < 40:
            raise DesktopBridgeError("invalid_device_token", "Invalid device token", status_code=401)
        row = await self.store.device_by_token_hash(_token_hash(token))
        if not row or row.get("status") != "active":
            raise DesktopBridgeError("invalid_device_token", "Invalid device token", status_code=401)
        if expected_device_id is not None:
            expected_device_id = _require_uuid(
                expected_device_id,
                code="invalid_device_id",
                detail="Desktop device id is invalid",
            )
        if expected_device_id is not None and str(row["id"]) != expected_device_id:
            raise DesktopBridgeError("device_token_mismatch", "Device token does not match this device", status_code=403)
        return row

    async def heartbeat(
        self,
        device: dict[str, Any],
        *,
        client_version: str | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        values: dict[str, Any] = {"last_seen_at": now, "updated_at": now}
        if client_version is not None:
            values["client_version"] = client_version.strip()[:80] or None
        if capabilities is not None:
            values["capabilities"] = _bounded_capabilities(capabilities)
        return _public_device(await self.store.touch_device(str(device["id"]), values))

    async def list_devices(
        self, *, organization_id: str, member_id: str | None
    ) -> list[dict[str, Any]]:
        return [_public_device(row) for row in await self.store.list_devices(organization_id, member_id)]

    async def get_device(
        self, *, device_id: str, organization_id: str, member_id: str | None
    ) -> dict[str, Any]:
        device_id = _require_uuid(
            device_id, code="invalid_device_id", detail="Desktop device id is invalid"
        )
        row = await self.store.get_device(device_id, organization_id, member_id)
        if not row:
            raise DesktopBridgeError("device_not_found", "Desktop device not found", status_code=404)
        return row

    async def revoke_device(
        self, *, device_id: str, organization_id: str, member_id: str | None, actor_id: str
    ) -> dict[str, Any]:
        device_id = _require_uuid(
            device_id, code="invalid_device_id", detail="Desktop device id is invalid"
        )
        row = await self.store.revoke_device(device_id, organization_id, member_id, _now())
        if not row:
            raise DesktopBridgeError("device_not_found", "Desktop device not found", status_code=404)
        await self._audit("desktop_device_revoked", actor_id, organization_id, device_id)
        return _public_device(row)

    async def disconnect(self, device: dict[str, Any]) -> dict[str, Any]:
        """Let a device atomically revoke itself before deleting local credentials."""

        return await self.revoke_device(
            device_id=str(device["id"]),
            organization_id=str(device["organization_id"]),
            member_id=str(device["member_id"]),
            actor_id=str(device["member_id"]),
        )

    @staticmethod
    def _validate_client_grant_id(value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
            raise DesktopBridgeError("invalid_client_grant_id", "Client grant id must be an opaque identifier")
        return value

    @staticmethod
    def _validate_display_name(value: str) -> str:
        value = value.strip()
        if (
            not value
            or len(value) > 120
            or "/" in value
            or "\\" in value
            or _has_control_characters(value)
        ):
            raise DesktopBridgeError("invalid_display_name", "Folder display name must not contain a path")
        return value

    async def register_grant(
        self,
        device: dict[str, Any],
        *,
        client_grant_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        client_grant_id = self._validate_client_grant_id(client_grant_id)
        display_name = self._validate_display_name(display_name)
        now = _now()
        row = await self.store.register_grant(
            {
                "id": str(uuid.uuid4()),
                "organization_id": str(device["organization_id"]),
                "region": settings.region,
                "member_id": str(device["member_id"]),
                "task_id": None,
                "folder_path": None,
                "purpose": "Desktop folder authorization",
                "status": "active",
                "allowed_commands": [],
                "allowed_apps": [],
                "device_id": str(device["id"]),
                "client_grant_id": client_grant_id,
                "folder_display_name": display_name,
                "created_at": now,
                "updated_at": now,
                "revoked_at": None,
            }
        )
        await self._audit(
            "desktop_folder_grant_registered",
            str(device["member_id"]),
            str(device["organization_id"]),
            str(device["id"]),
            grant_id=str(row["id"]),
        )
        return _public_grant(row)

    async def list_grants(
        self, *, organization_id: str, device_id: str, member_id: str | None
    ) -> list[dict[str, Any]]:
        device_id = _require_uuid(
            device_id, code="invalid_device_id", detail="Desktop device id is invalid"
        )
        await self.get_device(device_id=device_id, organization_id=organization_id, member_id=member_id)
        return [
            _public_grant(row)
            for row in await self.store.list_grants(organization_id, device_id, member_id)
        ]

    async def revoke_grant_from_device(
        self, device: dict[str, Any], *, client_grant_id: str
    ) -> dict[str, Any]:
        client_grant_id = self._validate_client_grant_id(client_grant_id)
        row = await self.store.revoke_grant(str(device["id"]), client_grant_id, _now())
        if not row:
            raise DesktopBridgeError("grant_not_found", "Folder grant not found", status_code=404)
        await self._audit(
            "desktop_folder_grant_revoked",
            str(device["member_id"]),
            str(device["organization_id"]),
            str(device["id"]),
            grant_id=str(row["id"]),
        )
        return _public_grant(row)

    async def revoke_grant_from_web(
        self,
        *,
        organization_id: str,
        member_id: str,
        grant_id: str,
        actor_id: str,
        notify_device: bool = True,
    ) -> dict[str, Any]:
        grant_id = _require_uuid(
            grant_id, code="invalid_grant_id", detail="Desktop folder grant id is invalid"
        )
        grant = await self.store.get_grant(grant_id, organization_id, member_id)
        if not grant:
            raise DesktopBridgeError("grant_not_found", "Folder grant not found", status_code=404)
        revocation_command: dict[str, Any] | None = None
        if notify_device and grant.get("status") == "active":
            # Queue before changing server state: enqueue intentionally requires
            # an active grant.  The client uses this command to delete its local
            # security-scoped bookmark when revocation starts from the web UI.
            revocation_command = await self.enqueue(
                organization_id=organization_id,
                member_id=member_id,
                grant_id=grant_id,
                command_type="revoke_grant",
                payload={},
            )
        row = await self.store.revoke_grant(str(grant["device_id"]), str(grant["client_grant_id"]), _now())
        if not row:
            raise DesktopBridgeError("grant_not_found", "Folder grant not found", status_code=404)
        await self._audit(
            "desktop_folder_grant_revoked",
            actor_id,
            organization_id,
            str(grant["device_id"]),
            grant_id=grant_id,
        )
        public = _public_grant(row)
        if revocation_command:
            public["revocation_command_id"] = revocation_command["command_id"]
        return public

    async def enqueue(
        self,
        *,
        organization_id: str,
        member_id: str,
        grant_id: str,
        command_type: str,
        payload: dict[str, Any],
        task_id: str | None = None,
        ttl_seconds: int = COMMAND_TTL_SECONDS,
    ) -> dict[str, Any]:
        if command_type not in COMMAND_TYPES:
            raise DesktopBridgeError("invalid_command_type", "Desktop command type is not supported")
        grant_id = _require_uuid(
            grant_id, code="invalid_grant_id", detail="Desktop folder grant id is invalid"
        )
        grant = await self.store.get_grant(grant_id, organization_id, member_id)
        if not grant or grant.get("status") != "active" or not grant.get("device_id"):
            raise DesktopBridgeError("grant_not_found", "Active desktop folder grant not found", status_code=404)
        device = await self.store.get_device(str(grant["device_id"]), organization_id, member_id)
        if not device or device.get("status") != "active":
            raise DesktopBridgeError("device_unavailable", "Paired desktop device is not active", status_code=503)
        raw = _json_bytes(
            {
                **payload,
                "grant_id": str(grant["id"]),
                "client_grant_id": str(grant["client_grant_id"]),
            }
        )
        now = _now()
        command = await self.store.enqueue_command(
            {
                "id": str(uuid.uuid4()),
                "organization_id": organization_id,
                "member_id": member_id,
                "device_id": str(device["id"]),
                "grant_id": str(grant["id"]),
                "task_id": task_id,
                "command_type": command_type,
                "payload": raw,
                "nonce": secrets.token_urlsafe(24),
                "status": "queued",
                "attempts": 0,
                "max_attempts": MAX_COMMAND_ATTEMPTS,
                "available_at": now,
                "expires_at": now + timedelta(seconds=max(15, min(int(ttl_seconds), 15 * 60))),
                "leased_at": None,
                "lease_expires_at": None,
                "result_nonce": None,
                "result_status": None,
                "result_error_code": None,
                "result_payload": None,
                "result_sha256": None,
                "completed_at": None,
                "created_at": now,
                "updated_at": now,
            }
        )
        await self._audit(
            "desktop_command_queued",
            member_id,
            organization_id,
            str(device["id"]),
            command_id=str(command["id"]),
            command_type=command_type,
            grant_id=grant_id,
        )
        return {
            "command_id": str(command["id"]),
            "device_id": str(command["device_id"]),
            "status": str(command["status"]),
            "expires_at": _iso(command["expires_at"]),
        }

    async def cancel_task_commands(
        self,
        *,
        organization_id: str,
        task_ids: list[str],
        actor_id: str,
    ) -> dict[str, Any]:
        """Cancel queued/leased commands and revoke task-scoped grants only."""

        scoped = sorted({str(task_id) for task_id in task_ids if task_id})
        if not scoped:
            return {"status": "complete", "commands": 0, "grants": 0}
        counts = await self.store.cancel_task_commands(
            organization_id, scoped, _now()
        )
        await self._audit(
            "desktop_task_commands_cancelled",
            actor_id,
            organization_id,
            scoped[0],
            task_ids=scoped,
            **counts,
        )
        return {"status": "complete", **counts}

    async def enqueue_notification(
        self,
        *,
        organization_id: str,
        member_id: str | None,
        title: str,
        body: str | None,
        category: str,
    ) -> dict[str, Any]:
        """Queue a narrow native notification for active tenant devices.

        This path intentionally cannot carry URLs, shell commands, actions, or
        arbitrary metadata.  Notification permission and preference checks live
        at ``core.notifications.emit``; this method only performs tenant/member
        device routing and durable signed queueing.
        """

        title = title.strip()
        normalized_body = (body or "").strip()
        category = category.strip().lower()
        if not title or len(title) > 120 or "\n" in title or "\r" in title:
            raise DesktopBridgeError(
                "invalid_notification_title", "Notification title must be 1-120 characters"
            )
        if len(normalized_body) > 500:
            raise DesktopBridgeError(
                "invalid_notification_body", "Notification body must be at most 500 characters"
            )
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,39}", category):
            raise DesktopBridgeError(
                "invalid_notification_category",
                "Notification category must be a safe 1-40 character identifier",
            )
        payload = _json_bytes(
            {"title": title, "body": normalized_body, "category": category}
        )
        now = _now()
        devices = [
            row
            for row in await self.store.list_devices(organization_id, member_id)
            if row.get("status") == "active"
        ]
        command_ids: list[str] = []
        for device in devices:
            command = await self.store.enqueue_command(
                {
                    "id": str(uuid.uuid4()),
                    "organization_id": organization_id,
                    "member_id": str(device["member_id"]),
                    "device_id": str(device["id"]),
                    "grant_id": None,
                    "command_type": "notify",
                    "payload": payload,
                    "nonce": secrets.token_urlsafe(24),
                    "status": "queued",
                    "attempts": 0,
                    "max_attempts": MAX_COMMAND_ATTEMPTS,
                    "available_at": now,
                    "expires_at": now + timedelta(minutes=5),
                    "leased_at": None,
                    "lease_expires_at": None,
                    "result_nonce": None,
                    "result_status": None,
                    "result_error_code": None,
                    "result_payload": None,
                    "result_sha256": None,
                    "completed_at": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            command_ids.append(str(command["id"]))
            await self._audit(
                "desktop_notification_queued",
                str(device["member_id"]),
                organization_id,
                str(device["id"]),
                command_id=str(command["id"]),
                category=category,
            )
        return {"status": "queued" if command_ids else "no_active_device", "queued": len(command_ids)}

    async def lease(
        self, device: dict[str, Any], *, limit: int = 1, lease_seconds: int = LEASE_SECONDS
    ) -> list[dict[str, Any]]:
        now = _now()
        await self.store.touch_device(str(device["id"]), {"last_seen_at": now, "updated_at": now})
        rows = await self.store.lease_commands(
            str(device["id"]),
            now,
            now + timedelta(seconds=max(10, min(int(lease_seconds), 60))),
            max(1, min(int(limit), 10)),
        )
        secret = reveal_command_secret(
            str(device["encrypted_command_secret"]),
            organization_id=str(device["organization_id"]),
            device_id=str(device["id"]),
        )
        envelopes: list[dict[str, Any]] = []
        for row in rows:
            payload = bytes(row["payload"])
            expires_at = _iso(row["expires_at"])
            signature = _sign(
                secret,
                command_signing_message(
                    command_id=str(row["id"]),
                    device_id=str(device["id"]),
                    nonce=str(row["nonce"]),
                    command_type=str(row["command_type"]),
                    expires_at=expires_at,
                    payload=payload,
                ),
            )
            envelopes.append(
                {
                    "version": "v1",
                    "command_id": str(row["id"]),
                    "device_id": str(device["id"]),
                    "nonce": str(row["nonce"]),
                    "command_type": str(row["command_type"]),
                    "expires_at": expires_at,
                    "payload_b64": base64.b64encode(payload).decode("ascii"),
                    "signature": signature,
                    "attempt": int(row["attempts"]),
                    "lease_expires_at": _iso(row["lease_expires_at"]),
                }
            )
        return envelopes

    async def submit_result(
        self,
        device: dict[str, Any],
        *,
        command_id: str,
        nonce: str,
        status: str,
        error_code: str | None,
        result_b64: str,
        signature: str,
    ) -> dict[str, Any]:
        if status not in RESULT_STATUSES:
            raise DesktopBridgeError("invalid_result_status", "Result status must be succeeded or failed")
        command_id = _require_uuid(
            command_id, code="invalid_command_id", detail="Desktop command id is invalid"
        )
        error_code = error_code or None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,159}", nonce):
            raise DesktopBridgeError("invalid_result_nonce", "Result nonce is invalid")
        if error_code is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", error_code
        ):
            raise DesktopBridgeError("invalid_error_code", "Result error code is invalid")
        result = _decode_b64(result_b64, maximum=MAX_RESULT_BYTES, code="invalid_result_payload")
        command = await self.store.get_command(command_id, str(device["id"]))
        if not command:
            raise DesktopBridgeError("command_not_found", "Command not found", status_code=404)
        secret = reveal_command_secret(
            str(device["encrypted_command_secret"]),
            organization_id=str(device["organization_id"]),
            device_id=str(device["id"]),
        )
        expected = _sign(
            secret,
            result_signing_message(
                command_id=command_id,
                device_id=str(device["id"]),
                nonce=nonce,
                status=status,
                error_code=error_code,
                result=result,
            ),
        )
        if not hmac.compare_digest(expected, signature.lower()):
            raise DesktopBridgeError("invalid_result_signature", "Invalid result signature", status_code=401)
        disposition, completed = await self.store.complete_command(
            command_id,
            str(device["id"]),
            _now(),
            {
                "result_nonce": nonce,
                "result_status": status,
                "result_error_code": error_code,
                "result_payload": result,
                "result_sha256": _sha256(result),
            },
        )
        await self._audit(
            "desktop_command_completed",
            str(device["member_id"]),
            str(device["organization_id"]),
            str(device["id"]),
            command_id=command_id,
            status=status,
            idempotent=disposition == "idempotent",
        )
        return {
            "command_id": command_id,
            "status": str(completed["status"]),
            "accepted": True,
            "idempotent": disposition == "idempotent",
        }

    async def command_result(self, command_id: str) -> dict[str, Any] | None:
        row = await self.store.get_command(command_id)
        if not row:
            return None
        result: dict[str, Any] = {
            "command_id": str(row["id"]),
            "status": str(row["status"]),
            "attempts": int(row["attempts"]),
            "expires_at": _iso(row["expires_at"]),
        }
        if row["status"] in RESULT_STATUSES:
            raw = bytes(row.get("result_payload") or b"")
            result["error_code"] = row.get("result_error_code")
            try:
                result["result"] = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                result["result_b64"] = base64.b64encode(raw).decode("ascii")
        return result

    async def wait_for_result(self, command_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + max(0.0, min(timeout_seconds, 60.0))
        while True:
            state = await self.command_result(command_id)
            if state is None:
                raise DesktopBridgeError("command_not_found", "Command not found", status_code=404)
            if state["status"] in RESULT_STATUSES | {"expired", "cancelled"}:
                return state
            if asyncio.get_running_loop().time() >= deadline:
                return state
            await asyncio.sleep(0.2)

    async def health(self) -> dict[str, Any]:
        checked = _now()
        try:
            active, fresh = await self.store.device_health_counts(
                checked - timedelta(minutes=2)
            )
        except Exception:
            return {
                "status": "unavailable",
                "tier": "unavailable",
                "configured": True,
                "verified": False,
                "checked_at": _iso(checked),
                "verified_at": None,
                "stale": True,
                "latency_ms": None,
                "error_code": "bridge_store_unavailable",
                "reason": "Desktop bridge storage is unavailable.",
                "setup": "Restore the database and apply migration 0051_desktop_device_bridge.",
            }
        if fresh:
            return {
                "status": "live",
                "tier": "live",
                "configured": True,
                "verified": True,
                "checked_at": _iso(checked),
                "verified_at": _iso(checked),
                "stale": False,
                "latency_ms": None,
                "error_code": None,
                "reason": "An authenticated desktop device checked in recently.",
                "setup": None,
                "verification_source": "paired_device_registry",
            }
        if active:
            return {
                "status": "degraded",
                "tier": "configured",
                "configured": True,
                "verified": False,
                "checked_at": _iso(checked),
                "verified_at": None,
                "stale": True,
                "latency_ms": None,
                "error_code": "device_heartbeat_stale",
                "reason": "Paired desktop devices exist, but none checked in during the last two minutes.",
                "setup": "Open the Chronos desktop app and restore its network connection.",
                "verification_source": "paired_device_registry",
            }
        return {
            "status": "configured",
            "tier": "configured",
            "configured": True,
            "verified": False,
            "checked_at": _iso(checked),
            "verified_at": None,
            "stale": True,
            "latency_ms": None,
            "error_code": "no_active_device",
            "reason": "Desktop bridge is configured, but no active device is paired.",
            "setup": "Pair the Chronos desktop app from the Devices settings page.",
        }


desktop_bridge = DesktopBridgeService()
