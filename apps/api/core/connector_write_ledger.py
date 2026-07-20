"""Durable, tenant-bound execution ledger for generic connector mutations.

The ledger is the safety boundary between Chronos retries and providers.  A
write is claimed before dispatch, provider evidence is committed before the
operation is completed, and an expired claim is only replayable when the
provider supplies an idempotency or reconciliation primitive.  Otherwise the
operation stops in ``manual_review`` so a possibly-completed write is never
blindly repeated.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from core.audit_redaction import redact
from core.config import settings


LEASE_SECONDS = 90
REENQUEUE_AFTER_SECONDS = 60
COMPLETED_RETENTION_DAYS = 30
MANUAL_REVIEW_RETENTION_DAYS = 180

_READ_METHODS = {"GET", "HEAD", "OPTIONS"}
_ERROR_SECRET = re.compile(
    r"(?i)(authorization|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|secret|password)\s*[:=]\s*\S+"
)


class WriteOperationConflict(RuntimeError):
    """An idempotency key was reused with a different immutable binding."""


class WriteOperationBusy(RuntimeError):
    """Another worker owns an unexpired claim."""


class ManualReviewRequired(RuntimeError):
    """A provider mutation may have completed and cannot be retried safely."""


class WriteOperationTerminal(RuntimeError):
    """The operation reached a non-success terminal state."""


@dataclass(frozen=True)
class ClaimOutcome:
    kind: Literal["dispatch", "replay"]
    operation: dict[str, Any]
    result: dict[str, Any] | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def secret_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_error(value: BaseException | str | None) -> str | None:
    if value is None:
        return None
    text = _ERROR_SECRET.sub(r"\1=[REDACTED]", str(value))
    # Provider URLs frequently contain object ids or signed query strings.  The
    # durable ledger needs a diagnosis category, not the request destination.
    text = re.sub(r"https?://\S+", "[REMOTE_URL]", text)
    return str(redact(text))[:1_000]


def provider_supports_idempotency(provider: str, *, header: str | None = None) -> bool:
    """Return true only for provider contracts Chronos can actually exercise."""

    return provider.strip().lower() == "stripe" or bool(header)


def is_http_mutation(method: str | None) -> bool:
    return str(method or "GET").upper() not in _READ_METHODS


def is_broker_connector_mutation(tool: str, args: dict[str, Any], *, composio: bool) -> bool:
    """Identify generic SaaS writes without wrapping specialized delivery paths."""

    if tool in {"gmail.send", "repo.create_pr"}:
        return False
    if tool == "platform.invoke":
        # platform.invoke delegates back through ToolBroker; wrapping both levels
        # would create two ledgers for one provider request.
        return False
    if tool.endswith(".api"):
        return is_http_mutation(str(args.get("method") or "GET"))
    lowered = tool.lower()
    provider = tool.split(".", 1)[0]
    write_markers = (
        ".draft",
        ".send",
        ".post",
        ".publish",
        ".write",
        ".create",
        ".update",
        ".delete",
        ".upload",
        ".move",
        ".copy",
        ".archive",
        ".reply",
        ".invite",
        ".assign",
        ".upsert",
        ".remove",
    )
    local_state_providers = {
        "browser",
        "chat_history",
        "code",
        "computer",
        "data",
        "desktop",
        "doc",
        "fs",
        "image",
        "local_computer",
        "platform",
        "repo",
        "skill",
        "voice",
    }
    if provider not in local_state_providers and any(
        marker in lowered for marker in write_markers
    ):
        return True
    if tool.startswith("mcp."):
        write_markers = (
            "send",
            "post",
            "publish",
            "write",
            "create",
            "update",
            "delete",
            "upload",
            "move",
            "copy",
            "execute",
        )
        read_markers = ("search", "list", "get", "read", "fetch", "query", "find", "inspect")
        if any(marker in lowered for marker in write_markers):
            return True
        return not any(marker in lowered for marker in read_markers)
    if not composio and not tool.startswith("mcp."):
        return False
    write = any(marker in lowered for marker in write_markers)
    if write:
        return True
    read_markers = (
        ".search",
        ".list",
        ".get",
        ".read",
        ".fetch",
        ".query",
        ".find",
        ".inspect",
        ".history",
    )
    return not any(marker in lowered for marker in read_markers)


def _cipher() -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - production dependency
        raise RuntimeError("cryptography package is required for connector outbox encryption") from exc
    configured_key = settings.vault_encryption_key
    if not configured_key:
        if settings.is_production:
            raise RuntimeError("VAULT_ENCRYPTION_KEY is required for durable connector writes")
        # Local/test installations remain restart-stable without weakening the
        # production validation boundary. Production rejects the default JWT
        # secret and always requires the dedicated 256-bit vault key.
        configured_key = hashlib.sha256(
            f"chronos-dev-connector-outbox:{settings.jwt_secret}".encode()
        ).hexdigest()
    key = bytes.fromhex(configured_key)
    if len(key) != 32:
        raise RuntimeError("VAULT_ENCRYPTION_KEY must be 64 hexadecimal characters")
    return AESGCM(key)


def encrypt_outbox(payload: dict[str, Any], *, organization_id: str, operation_id: str) -> str:
    nonce = secrets.token_bytes(12)
    plaintext = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    aad = f"connector-write:v1:{organization_id}:{operation_id}".encode("utf-8")
    ciphertext = _cipher().encrypt(nonce, plaintext, aad)
    return "v1:" + (nonce + ciphertext).hex()


def decrypt_outbox(value: str, *, organization_id: str, operation_id: str) -> dict[str, Any]:
    version, _, encoded = value.partition(":")
    if version != "v1" or not encoded:
        raise RuntimeError("Unsupported connector outbox ciphertext")
    raw = bytes.fromhex(encoded)
    aad = f"connector-write:v1:{organization_id}:{operation_id}".encode("utf-8")
    plaintext = _cipher().decrypt(raw[:12], raw[12:], aad)
    decoded = json.loads(plaintext.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise RuntimeError("Connector outbox payload is invalid")
    return decoded


class ConnectorWriteLedger:
    """State machine backed by either repository implementation."""

    def __init__(self, repository: Any) -> None:
        self.repository = repository

    async def prepare(
        self,
        *,
        organization_id: str,
        member_id: str,
        task_id: str,
        channel: str,
        tool: str,
        provider: str,
        risk_level: str,
        payload: dict[str, Any],
        approval_binding: str,
        idempotency_key: str,
        connector_job_id: str | None = None,
        provider_idempotency: bool = False,
        supports_reconciliation: bool = False,
        outbox_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not all((organization_id, member_id, task_id, tool, idempotency_key)):
            raise WriteOperationConflict("Connector write scope is incomplete")
        operation_id = str(uuid.uuid4())
        values: dict[str, Any] = {
            "id": operation_id,
            "organization_id": organization_id,
            "region": getattr(settings, "region", "us"),
            "member_id": member_id,
            "task_id": task_id,
            "connector_job_id": connector_job_id,
            "channel": channel,
            "tool": tool,
            "provider": provider,
            "risk_level": risk_level,
            "payload_sha256": canonical_sha256(payload),
            "approval_binding": secret_sha256(approval_binding),
            "idempotency_sha256": secret_sha256(idempotency_key),
            "provider_idempotency_key": f"cw_{uuid.UUID(operation_id).hex}",
            "provider_supports_idempotency": bool(provider_idempotency),
            "supports_reconciliation": bool(supports_reconciliation),
            "status": "pending",
            "expires_at": utcnow() + timedelta(days=COMPLETED_RETENTION_DAYS),
        }
        if outbox_payload is not None:
            values["encrypted_payload"] = encrypt_outbox(
                outbox_payload,
                organization_id=organization_id,
                operation_id=operation_id,
            )
        operation = await self.repository.create_or_get_write_operation(**values)
        immutable = (
            "organization_id",
            "member_id",
            "task_id",
            "channel",
            "tool",
            "provider",
            "payload_sha256",
            "approval_binding",
            "idempotency_sha256",
        )
        if any(str(operation.get(key)) != str(values.get(key)) for key in immutable):
            raise WriteOperationConflict(
                "Idempotency key is already bound to a different connector write"
            )
        return {**operation, "_created": str(operation.get("id")) == operation_id}

    async def claim(
        self,
        operation_id: str,
        *,
        organization_id: str,
        owner: str,
        lease_seconds: int = LEASE_SECONDS,
    ) -> ClaimOutcome:
        operation = await self.repository.get_write_operation(
            operation_id, organization_id=organization_id
        )
        if not operation:
            raise WriteOperationConflict("Connector write operation was not found in this tenant")
        status = str(operation.get("status") or "")
        if status == "complete":
            return ClaimOutcome("replay", operation, operation.get("result") or {})
        if status == "provider_confirmed":
            adopted = await self.repository.update_write_operation(
                operation_id,
                organization_id=organization_id,
                expected_statuses=["provider_confirmed"],
                status="complete",
                completed_at=utcnow(),
                claim_owner=None,
                claim_expires_at=None,
            )
            operation = adopted or operation
            return ClaimOutcome("replay", operation, operation.get("result") or {})
        if status == "manual_review":
            raise ManualReviewRequired(operation.get("last_error") or "Connector write requires manual review")
        if status in {"failed", "cancelled"}:
            raise WriteOperationTerminal(operation.get("last_error") or f"Connector write is {status}")

        now = utcnow()
        claim_expires = operation.get("claim_expires_at")
        if isinstance(claim_expires, str):
            claim_expires = datetime.fromisoformat(claim_expires)
        expired_claim = status == "claimed" and (not claim_expires or claim_expires <= now)
        if status == "claimed" and not expired_claim:
            raise WriteOperationBusy("Connector write is already claimed")
        if expired_claim and not (
            operation.get("provider_supports_idempotency")
            or operation.get("supports_reconciliation")
        ):
            await self.repository.update_write_operation(
                operation_id,
                organization_id=organization_id,
                expected_statuses=["claimed"],
                status="manual_review",
                last_error="Provider outcome is ambiguous after worker interruption; retry is disabled",
                claim_owner=None,
                claim_expires_at=None,
                expires_at=now + timedelta(days=MANUAL_REVIEW_RETENTION_DAYS),
            )
            raise ManualReviewRequired(
                "Provider outcome is ambiguous after worker interruption; manual review is required"
            )

        claimed = await self.repository.claim_write_operation(
            operation_id,
            organization_id=organization_id,
            owner=owner,
            lease_expires_at=now + timedelta(seconds=max(1, lease_seconds)),
            now=now,
        )
        if not claimed:
            raise WriteOperationBusy("Connector write claim was acquired by another worker")
        return ClaimOutcome("dispatch", claimed)

    async def record_provider_response(
        self,
        operation_id: str,
        *,
        organization_id: str,
        result: dict[str, Any],
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_result = redact(result)
        row = await self.repository.update_write_operation(
            operation_id,
            organization_id=organization_id,
            expected_statuses=["claimed", "provider_confirmed"],
            status="provider_confirmed",
            result=safe_result,
            provider_evidence=redact(evidence or {}),
            provider_responded_at=utcnow(),
        )
        if not row:
            raise WriteOperationConflict("Connector write response could not be recorded")
        return row

    async def complete(self, operation_id: str, *, organization_id: str) -> dict[str, Any]:
        row = await self.repository.update_write_operation(
            operation_id,
            organization_id=organization_id,
            expected_statuses=["provider_confirmed"],
            status="complete",
            completed_at=utcnow(),
            claim_owner=None,
            claim_expires_at=None,
        )
        if not row:
            raise WriteOperationConflict("Connector write completion could not be committed")
        return row

    async def mark_ambiguous(
        self,
        operation_id: str,
        *,
        organization_id: str,
        error: BaseException | str,
    ) -> dict[str, Any]:
        operation = await self.repository.get_write_operation(
            operation_id, organization_id=organization_id
        )
        if not operation:
            raise WriteOperationConflict("Connector write operation was not found")
        safe = safe_error(error) or "Provider outcome is ambiguous"
        can_retry = bool(
            operation.get("provider_supports_idempotency")
            or operation.get("supports_reconciliation")
        )
        return await self.repository.update_write_operation(
            operation_id,
            organization_id=organization_id,
            expected_statuses=["claimed", "retry"],
            status="retry" if can_retry else "manual_review",
            last_error=safe,
            next_attempt_at=utcnow() if can_retry else None,
            claim_owner=None,
            claim_expires_at=None,
            expires_at=utcnow()
            + timedelta(
                days=COMPLETED_RETENTION_DAYS if can_retry else MANUAL_REVIEW_RETENTION_DAYS
            ),
        )

    async def mark_failed(
        self,
        operation_id: str,
        *,
        organization_id: str,
        error: BaseException | str,
    ) -> dict[str, Any]:
        return await self.repository.update_write_operation(
            operation_id,
            organization_id=organization_id,
            expected_statuses=["pending", "claimed", "retry"],
            status="failed",
            last_error=safe_error(error),
            completed_at=utcnow(),
            claim_owner=None,
            claim_expires_at=None,
        )


async def recover_framework_outbox(
    repository: Any,
    queue: Any,
    *,
    limit: int = 100,
) -> dict[str, int]:
    """Rebuild Redis queue entries from encrypted Postgres outbox payloads."""

    now = utcnow()
    operations = await repository.list_recoverable_write_operations(
        now=now,
        reenqueue_before=now - timedelta(seconds=REENQUEUE_AFTER_SECONDS),
        limit=limit,
    )
    recovered = 0
    manual_review = 0
    ledger = ConnectorWriteLedger(repository)
    for operation in operations:
        operation_id = str(operation["id"])
        if (
            operation.get("status") == "claimed"
            and not operation.get("provider_supports_idempotency")
            and not operation.get("supports_reconciliation")
        ):
            try:
                await ledger.claim(
                    operation_id,
                    organization_id=str(operation["organization_id"]),
                    owner="outbox-recovery",
                )
            except ManualReviewRequired:
                manual_review += 1
            except (WriteOperationBusy, WriteOperationTerminal):
                pass
            continue
        encrypted = operation.get("encrypted_payload")
        if not encrypted:
            continue
        payload = decrypt_outbox(
            str(encrypted),
            organization_id=str(operation["organization_id"]),
            operation_id=operation_id,
        )
        payload["write_operation_id"] = operation_id
        if hasattr(repository, "get_execution_job"):
            stored_job = await repository.get_execution_job(
                str(payload["id"]), tenant_id=str(operation["organization_id"])
            )
            if not stored_job:
                try:
                    await repository.create_execution_job(
                        id=str(payload["id"]),
                        tenant_id=str(operation["organization_id"]),
                        task_id=payload.get("task_id"),
                        workspace_id=payload.get("workspace_id") or "default",
                        employee_id=payload.get("employee_id")
                        or str(operation["member_id"]),
                        user_id=payload.get("user_id")
                        or str(operation["member_id"]),
                        connector_id=str(payload["connector_id"]),
                        action_name=str(payload["action_name"]),
                        arguments=dict(payload.get("arguments") or {}),
                        max_attempts=int(payload.get("max_attempts") or 1),
                        timeout_ms=int(payload.get("timeout_ms") or 15000),
                        write_operation_id=operation_id,
                        approval_id=payload.get("approval_id"),
                    )
                except Exception:
                    # A concurrent reaper may have inserted the same immutable
                    # job id between the lookup and insert. Verify that race;
                    # any other persistence failure must stop recovery.
                    stored_job = await repository.get_execution_job(
                        str(payload["id"]),
                        tenant_id=str(operation["organization_id"]),
                    )
                    if not stored_job:
                        raise
        await queue.enqueue(payload)
        await repository.mark_write_operation_enqueued(
            operation_id,
            organization_id=str(operation["organization_id"]),
            enqueued_at=now,
        )
        recovered += 1
    purged = await repository.purge_expired_write_operations(now=now, limit=limit)
    return {"recovered": recovered, "manual_review": manual_review, "purged": purged}
