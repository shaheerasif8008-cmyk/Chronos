"""Provider-safe lifecycle and ingress controls for published agents."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from connectors import vault
from core import audit, permissions
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from core.redis import redis_client


TARGETS = {"slack", "teams", "email", "web", "api"}
PROVIDERS = {"slack", "teams"}
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9._:@/=-]{1,240}$")
_MAX_TEXT = 16_000
_PUBLIC_CONFIG_KEYS = {"reply_mode"}


def _public(row: dict[str, Any]) -> dict[str, Any]:
    hidden = {"secret_vault_ref"}
    result = {key: value for key, value in row.items() if key not in hidden}
    for key, value in list(result.items()):
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif key.endswith("_id") and value is not None:
            result[key] = str(value)
    result["id"] = str(row["id"])
    if row.get("agent_profile_id") is not None:
        result["agent_profile_id"] = str(row["agent_profile_id"])
    return result


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


def _normalize_origin(raw: str) -> str:
    parsed = urlparse(raw.strip())
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=422, detail="Embed origins must be bare origins")
    if parsed.scheme != "https" and not (not settings.is_production and local and parsed.scheme == "http"):
        raise HTTPException(status_code=422, detail="Embed origins must use HTTPS")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _safe_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="Publication config must be an object")
    unknown = sorted(set(map(str, value)) - (_PUBLIC_CONFIG_KEYS | {"allowed_origins", "binding_id", "rate_limit_per_minute"}))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unsupported publication config: {', '.join(unknown)}")
    reply_mode = str(value.get("reply_mode") or "threaded").strip().lower()
    if reply_mode not in {"threaded", "direct"}:
        raise HTTPException(status_code=422, detail="Reply mode must be threaded or direct")
    clean = {"reply_mode": reply_mode}
    if len(json.dumps(clean)) > 2_000:
        raise HTTPException(status_code=422, detail="Publication config is too large")
    return clean


async def _connector_for_binding(member: Member, provider: str, connector_id: str) -> dict[str, Any]:
    connectors = await reflect_table("connectors")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(connectors).where(
                    connectors.c.id == connector_id,
                    connectors.c.organization_id == member.organization_id,
                    connectors.c.provider == provider,
                    connectors.c.status == "active",
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=422, detail=f"Connect {provider} before binding a channel")
    owner = row.get("member_id")
    if owner and str(owner) != member.id and member.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="That connector belongs to another member")
    return dict(row)


async def create_binding(member: Member, data: dict[str, Any]) -> dict[str, Any]:
    await permissions.check(member, "publish_agent", str(member.organization_id))
    provider = str(data.get("provider") or "").lower()
    connector_id = str(data.get("connector_id") or "")
    channel_id = str(data.get("external_channel_id") or "").strip()
    tenant_id = str(data.get("external_tenant_id") or "").strip() or None
    if provider not in PROVIDERS:
        raise HTTPException(status_code=422, detail="Provider must be slack or teams")
    if not connector_id or not _OPAQUE_ID.fullmatch(channel_id):
        raise HTTPException(status_code=422, detail="A valid connector and channel ID are required")
    if tenant_id is None or not _OPAQUE_ID.fullmatch(tenant_id):
        raise HTTPException(status_code=422, detail="A valid external workspace or tenant ID is required")
    connector = await _connector_for_binding(member, provider, connector_id)
    bindings = await reflect_table("agent_publication_bindings")
    values = {
        "organization_id": member.organization_id,
        "region": member.region,
        "member_id": str(connector.get("member_id") or member.id),
        "provider": provider,
        "connector_id": connector_id,
        "external_tenant_id": tenant_id,
        "external_channel_id": channel_id,
        "display_name": str(data.get("display_name") or channel_id)[:120],
        "status": "active",
        "provider_status": "ready",
        "created_by": member.id,
    }
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                select(bindings).where(
                    bindings.c.organization_id == member.organization_id,
                    bindings.c.provider == provider,
                    bindings.c.external_tenant_id.is_(tenant_id) if tenant_id is None else bindings.c.external_tenant_id == tenant_id,
                    bindings.c.external_channel_id == channel_id,
                )
            )
        ).mappings().first()
        if existing:
            row = (
                await conn.execute(
                    update(bindings)
                    .where(bindings.c.id == existing["id"])
                    .values(**values, updated_at=datetime.now(timezone.utc), revoked_at=None)
                    .returning(bindings)
                )
            ).mappings().one()
        else:
            row = (await conn.execute(insert(bindings).values(**values).returning(bindings))).mappings().one()
    await audit.log("agent_channel_bound", member.id, "agents.publications.bind", organization_id=member.organization_id, resource_type="agent_publication_binding", resource_id=str(row["id"]), payload={"provider": provider, "channel_id_hash": hashlib.sha256(channel_id.encode()).hexdigest()})
    return _public(dict(row))


async def list_bindings(member: Member) -> list[dict[str, Any]]:
    await permissions.check(member, "publish_agent", member.organization_id)
    bindings = await reflect_table("agent_publication_bindings")
    async with engine.begin() as conn:
        rows = (await conn.execute(select(bindings).where(bindings.c.organization_id == member.organization_id).order_by(bindings.c.created_at.desc()))).mappings().all()
    return [_public(dict(row)) for row in rows]


async def revoke_binding(member: Member, binding_id: str) -> dict[str, Any]:
    await permissions.check(member, "publish_agent", binding_id)
    bindings = await reflect_table("agent_publication_bindings")
    publications = await reflect_table("agent_publications")
    receipts = await reflect_table("notification_delivery_receipts")
    approvals = await reflect_table("approvals")
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                update(bindings).where(bindings.c.id == binding_id, bindings.c.organization_id == member.organization_id)
                .values(status="revoked", provider_status="degraded", revoked_at=now, updated_at=now)
                .returning(bindings)
            )
        ).mappings().first()
        if row:
            publication_ids = (
                await conn.execute(
                    select(publications.c.id).where(
                        publications.c.organization_id == member.organization_id,
                        publications.c.binding_id == binding_id,
                        publications.c.status == "active",
                    )
                )
            ).scalars().all()
            await conn.execute(
                update(publications)
                .where(
                    publications.c.organization_id == member.organization_id,
                    publications.c.binding_id == binding_id,
                    publications.c.status == "active",
                )
                .values(
                    status="disabled",
                    provider_status="degraded",
                    last_error_code="binding_revoked",
                    unpublished_at=now,
                    updated_at=now,
                )
            )
            if publication_ids:
                approval_ids = (
                    await conn.execute(
                        select(receipts.c.approval_id).where(
                            receipts.c.organization_id == member.organization_id,
                            receipts.c.publication_id.in_(publication_ids),
                            receipts.c.status.in_(["approval_pending", "pending", "retry", "processing"]),
                            receipts.c.approval_id.is_not(None),
                        )
                    )
                ).scalars().all()
                await conn.execute(
                    update(receipts)
                    .where(
                        receipts.c.organization_id == member.organization_id,
                        receipts.c.publication_id.in_(publication_ids),
                        receipts.c.status.in_(["approval_pending", "pending", "retry", "processing"]),
                    )
                    .values(
                        status="dead_letter",
                        last_error_code="binding_revoked",
                        next_attempt_at=None,
                        claimed_at=None,
                        claim_token=None,
                        updated_at=now,
                    )
                )
                if approval_ids:
                    await conn.execute(
                        update(approvals)
                        .where(
                            approvals.c.organization_id == member.organization_id,
                            approvals.c.id.in_(approval_ids),
                            approvals.c.status == "pending",
                        )
                        .values(
                            status="rejected",
                            decided_by=member.id,
                            decided_at=now,
                            decision_note="Provider binding was revoked",
                        )
                    )
    if row is None:
        raise HTTPException(status_code=404, detail="Channel binding not found")
    await audit.log(
        "agent_channel_binding_revoked",
        member.id,
        "agents.publications.bindings.revoke",
        organization_id=member.organization_id,
        resource_type="agent_publication_binding",
        resource_id=binding_id,
        payload={"provider": str(row["provider"])},
    )
    return _public(dict(row))


async def create_publication(member: Member, agent: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    await permissions.check(member, "publish_agent", str(agent["id"]))
    target = str(data.get("target") or "").lower()
    if target not in TARGETS:
        raise HTTPException(status_code=422, detail="Unsupported publish target")
    raw_config = data.get("config") or {}
    config = _safe_config(raw_config)
    try:
        rate_limit = int(raw_config.get("rate_limit_per_minute", 30))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Rate limit must be an integer") from exc
    if not 1 <= rate_limit <= 600:
        raise HTTPException(status_code=422, detail="Rate limit must be between 1 and 600")
    channel_id = str(data.get("external_channel_id") or "").strip() or None
    binding_id = str(data.get("binding_id") or raw_config.get("binding_id", "")).strip() or None
    bindings = await reflect_table("agent_publication_bindings")
    binding: dict[str, Any] | None = None
    if target in PROVIDERS:
        if not binding_id:
            raise HTTPException(status_code=422, detail=f"Bind an authorized {target} channel before publishing")
        async with engine.begin() as conn:
            binding_row = (
                await conn.execute(select(bindings).where(bindings.c.id == binding_id, bindings.c.organization_id == member.organization_id, bindings.c.provider == target, bindings.c.status == "active"))
            ).mappings().first()
        if binding_row is None:
            raise HTTPException(status_code=422, detail="Active provider binding not found")
        binding = dict(binding_row)
        channel_id = str(binding["external_channel_id"])
    if target == "email" and (channel_id is None or not _EMAIL.fullmatch(channel_id)):
        raise HTTPException(status_code=422, detail="A valid recipient email is required")
    raw_origins = raw_config.get("allowed_origins", [])
    if not isinstance(raw_origins, list) or len(raw_origins) > 20:
        raise HTTPException(status_code=422, detail="Allowed origins must be a list of at most 20 origins")
    allowed_origins = list(dict.fromkeys(_normalize_origin(str(item)) for item in raw_origins))
    if target == "web" and not allowed_origins:
        raise HTTPException(status_code=422, detail="At least one allowed embed origin is required")

    secret: str | None = None
    secret_ref: str | None = None
    if target == "web":
        secret = f"chr_embed_{secrets.token_urlsafe(36)}"
        secret_ref = await vault.store(connector_id=f"agent-publication:{agent['id']}", credentials={"kind": "agent_publication_secret", "secret": secret}, org_id=member.organization_id)
    provider_status = "ready"
    error_code = None
    if target == "email" and not (
        settings.sendgrid_api_key
        and settings.notification_from_email
        and settings.sendgrid_inbound_public_key
    ):
        provider_status, error_code = "degraded", "email_provider_not_configured"
    if target == "slack" and not settings.slack_signing_secret:
        provider_status, error_code = "degraded", "slack_signing_secret_not_configured"
    if target == "teams" and not settings.teams_bot_app_id:
        provider_status, error_code = "degraded", "teams_bot_identity_not_configured"

    publications = await reflect_table("agent_publications")
    values = {
        "organization_id": member.organization_id,
        "region": member.region,
        "agent_profile_id": agent["id"],
        "target": target,
        "display_name": str(data.get("display_name") or agent["name"])[:120],
        "external_channel_id": channel_id,
        "config": config,
        "approval_policy": agent.get("approval_policy") or {},
        "binding_id": binding_id,
        "secret_vault_ref": secret_ref,
        "secret_fingerprint": _fingerprint(secret) if secret else None,
        "allowed_origins": allowed_origins,
        "rate_limit_per_minute": rate_limit,
        "status": "active",
        "provider_status": provider_status,
        "last_error_code": error_code,
        "created_by": member.id,
    }
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(insert(publications).values(**values).returning(publications))).mappings().one()
    except IntegrityError as exc:
        if secret_ref:
            await vault.delete(secret_ref, actor_id=member.id, org_id=member.organization_id)
        detail = (
            "That provider channel already has an active published agent"
            if binding_id
            else "That email address already has an active published agent"
        )
        raise HTTPException(status_code=409, detail=detail) from exc
    except Exception:
        if secret_ref:
            await vault.delete(secret_ref, actor_id=member.id, org_id=member.organization_id)
        raise
    result = _public(dict(row))
    if secret:
        result["plaintext_secret"] = secret
    await audit.log("agent_published", member.id, "agents.publish", organization_id=member.organization_id, resource_type="agent_publication", resource_id=result["id"], payload={"agent_id": str(agent["id"]), "target": target, "provider_status": provider_status})
    return result


async def list_publications(member: Member, agent_id: str | None = None) -> list[dict[str, Any]]:
    await permissions.check(member, "publish_agent", agent_id or member.organization_id)
    table = await reflect_table("agent_publications")
    filters = [table.c.organization_id == member.organization_id]
    if agent_id:
        filters.append(table.c.agent_profile_id == agent_id)
    async with engine.begin() as conn:
        rows = (await conn.execute(select(table).where(*filters).order_by(table.c.created_at.desc()))).mappings().all()
    return [_public(dict(row)) for row in rows]


async def lifecycle(member: Member, publication_id: str, action: str) -> dict[str, Any]:
    await permissions.check(member, "publish_agent", publication_id)
    if action not in {"unpublish", "rotate", "revoke"}:
        raise HTTPException(status_code=422, detail="Unsupported lifecycle action")
    table = await reflect_table("agent_publications")
    receipts = await reflect_table("notification_delivery_receipts")
    approvals = await reflect_table("approvals")
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        current = (await conn.execute(select(table).where(table.c.id == publication_id, table.c.organization_id == member.organization_id))).mappings().first()
    if current is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    values: dict[str, Any] = {
        "updated_at": now,
        "secret_version": int(current["secret_version"] or 1) + 1,
    }
    plaintext: str | None = None
    replacement_ref: str | None = None
    old_ref = str(current.get("secret_vault_ref") or "") or None
    if action == "unpublish":
        values.update(status="disabled", unpublished_at=now)
    elif action == "revoke":
        values.update(
            status="revoked",
            provider_status="revoked",
            revoked_at=now,
            secret_vault_ref=None,
            secret_fingerprint=None,
        )
    else:
        values.update(status="active", provider_status="ready", last_error_code=None, unpublished_at=None)
        if current["target"] == "web":
            plaintext = f"chr_embed_{secrets.token_urlsafe(36)}"
            replacement_ref = await vault.store(
                connector_id=f"agent-publication:{publication_id}",
                credentials={"kind": "agent_publication_secret", "secret": plaintext},
                org_id=member.organization_id,
            )
            values["secret_vault_ref"] = replacement_ref
            values["secret_fingerprint"] = _fingerprint(plaintext)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(table)
                    .where(
                        table.c.id == publication_id,
                        table.c.organization_id == member.organization_id,
                        table.c.secret_version == int(current["secret_version"] or 1),
                    )
                    .values(**values)
                    .returning(table)
                )
            ).mappings().first()
            if row is not None and action in {"unpublish", "revoke"}:
                approval_ids = (
                    await conn.execute(
                        select(receipts.c.approval_id).where(
                            receipts.c.organization_id == member.organization_id,
                            receipts.c.publication_id == publication_id,
                            receipts.c.status.in_(["approval_pending", "pending", "retry", "processing"]),
                            receipts.c.approval_id.is_not(None),
                        )
                    )
                ).scalars().all()
                await conn.execute(
                    update(receipts)
                    .where(
                        receipts.c.organization_id == member.organization_id,
                        receipts.c.publication_id == publication_id,
                        receipts.c.status.in_(["approval_pending", "pending", "retry", "processing"]),
                    )
                    .values(
                        status="dead_letter",
                        last_error_code=f"publication_{action}",
                        next_attempt_at=None,
                        claimed_at=None,
                        claim_token=None,
                        updated_at=now,
                    )
                )
                if approval_ids:
                    await conn.execute(
                        update(approvals)
                        .where(
                            approvals.c.organization_id == member.organization_id,
                            approvals.c.id.in_(approval_ids),
                            approvals.c.status == "pending",
                        )
                        .values(
                            status="rejected",
                            decided_by=member.id,
                            decided_at=now,
                            decision_note=(
                                "Publication was unpublished"
                                if action == "unpublish"
                                else "Publication was revoked"
                            ),
                        )
                    )
        if row is None:
            raise HTTPException(status_code=409, detail="Publication changed concurrently; reload and try again")
    except IntegrityError as exc:
        if replacement_ref:
            await vault.delete(replacement_ref, actor_id=member.id, org_id=member.organization_id)
        raise HTTPException(status_code=409, detail="That provider channel already has an active published agent") from exc
    except Exception:
        if replacement_ref:
            await vault.delete(replacement_ref, actor_id=member.id, org_id=member.organization_id)
        raise
    if (action == "revoke" or replacement_ref) and old_ref and old_ref != replacement_ref:
        try:
            await vault.delete(old_ref, actor_id=member.id, org_id=member.organization_id)
        except Exception:
            # The active database state is already safe: revoked rows cannot be
            # invoked and rotations point only at the replacement credential.
            pass
    result = _public(dict(row))
    if plaintext:
        result["plaintext_secret"] = plaintext
    await audit.log(f"agent_publication_{action}", member.id, f"agents.publications.{action}", organization_id=member.organization_id, resource_type="agent_publication", resource_id=publication_id)
    return result


async def require_active(publication_id: str, *, target: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    publications = await reflect_table("agent_publications")
    profiles = await reflect_table("agent_profiles")
    async with engine.begin() as conn:
        publication = (await conn.execute(select(publications).where(publications.c.id == publication_id, publications.c.status == "active"))).mappings().first()
        if publication is None or (target and publication["target"] != target):
            raise HTTPException(status_code=404, detail="Publication not found")
        profile = (await conn.execute(select(profiles).where(profiles.c.id == publication["agent_profile_id"], profiles.c.organization_id == publication["organization_id"], profiles.c.status == "active"))).mappings().first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = dict(profile)
    agent["id"] = str(agent["id"])
    agent["project_ids"] = [str(value) for value in (agent.get("project_ids") or [])]
    for key, value in list(agent.items()):
        if hasattr(value, "isoformat"):
            agent[key] = value.isoformat()
    return _public(dict(publication)), agent


async def verify_embed(publication: dict[str, Any], token: str, origin: str | None) -> None:
    origins = set(publication.get("allowed_origins") or [])
    if not origin or _normalize_origin(origin) not in origins:
        raise HTTPException(status_code=403, detail="Embed origin is not allowed")
    ref = publication.get("secret_vault_ref")
    if not ref:
        raise HTTPException(status_code=503, detail="Embed credential must be rotated")
    stored = await vault.get(str(ref), org_id=str(publication["organization_id"]))
    if not token or not hmac.compare_digest(token, str(stored.get("secret") or "")):
        raise HTTPException(status_code=403, detail="Invalid embed credential")


async def enforce_rate_limit(publication: dict[str, Any]) -> None:
    bucket = int(time.time() // 60)
    key = f"agent-publication:rate:{publication['id']}:{bucket}"
    try:
        count = await redis_client.incr(key)
        if int(count) == 1:
            await redis_client.expire(key, 120)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Publication rate limiter unavailable") from exc
    if int(count) > int(publication.get("rate_limit_per_minute") or 30):
        raise HTTPException(status_code=429, detail="Publication rate limit exceeded")


def validate_message(text: str) -> str:
    clean = text.replace("\x00", "").strip()
    if not clean or len(clean) > _MAX_TEXT:
        raise HTTPException(status_code=422, detail="Message must be between 1 and 16000 characters")
    return clean
