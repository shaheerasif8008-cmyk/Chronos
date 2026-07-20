"""Durable notification email delivery and weekly digests.

Outbound email is driven from ``notification_delivery_receipts`` rather than
from an in-process loop.  Every recipient has a stable deduplication key, a
bounded attempt budget, retry timing, and a short-lived claim.  PostgreSQL
``FOR UPDATE SKIP LOCKED`` claims make concurrent API replicas safe.  A worker
crash after the provider accepts a message but before the receipt commit can
still cause a duplicate (SendGrid has no idempotent-send API); the stable
delivery key is sent as provider metadata so that boundary is observable.

When email is disabled or the provider is not configured, in-app delivery
continues and receipts remain pending.  Chronos never marks an email delivered
unless the provider accepted it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import and_, func, insert, or_, select, union_all, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.notifications import _notification_settings
from connectors import composio_client, generic_http

MAX_ATTEMPTS = 5
CLAIM_TIMEOUT = timedelta(minutes=10)
MAX_BACKOFF = timedelta(hours=6)
_SAFE_ERROR = re.compile(r"^[a-z0-9_:-]{1,96}$")
_PRIVATE_PROVIDER_METADATA_KEYS = {
    "body", "content", "email", "message", "password", "secret", "text", "token",
    "webhook", "authorization", "recipient", "credential",
}
log = logging.getLogger(__name__)


def _assert_metadata_only(value: dict[str, Any]) -> None:
    """Reject content or credentials before provider metadata becomes durable."""

    def walk(item: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(item, dict):
            for raw_key, child in item.items():
                key = str(raw_key).lower()
                if any(private in key for private in _PRIVATE_PROVIDER_METADATA_KEYS) and not key.endswith("_hash"):
                    raise ValueError(f"provider_payload contains private field: {'.'.join((*path, key))}")
                walk(child, (*path, key))
        elif isinstance(item, list):
            for child in item:
                walk(child, path)
        elif isinstance(item, str):
            lowered = item.lower()
            if "@" in item or lowered.startswith(("bearer ", "whsec_", "chr_embed_")):
                raise ValueError("provider_payload contains private value")

    walk(value)


class EmailNotConfigured(Exception):
    pass


class EmailDeliveryError(Exception):
    """A configured provider rejected or could not deliver a message."""


class ProviderDeliveryError(Exception):
    """A non-email provider rejected or could not deliver a message."""


class AmbiguousProviderDelivery(ProviderDeliveryError):
    """A provider may have accepted a non-idempotent external message."""


def email_is_configured() -> bool:
    return bool(settings.sendgrid_api_key and settings.notification_from_email)


def _provider_send_email(
    *,
    to: str,
    subject: str,
    body: str,
    delivery_key: str | None = None,
) -> str | None:
    """Send one email through SendGrid and return its non-secret message id."""

    if not email_is_configured():
        raise EmailNotConfigured("Email provider is not configured")
    personalization: dict[str, Any] = {"to": [{"email": to}]}
    if delivery_key:
        personalization["custom_args"] = {"chronos_delivery_key": delivery_key}
    try:
        response = httpx.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {settings.sendgrid_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "personalizations": [personalization],
                "from": {"email": settings.notification_from_email},
                "subject": subject,
                "content": [{"type": "text/plain", "value": body}],
            },
            timeout=15.0,
            follow_redirects=False,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        raise EmailDeliveryError("email_provider_unreachable") from exc
    except httpx.HTTPError as exc:
        raise AmbiguousProviderDelivery("ambiguous_provider_outcome") from exc
    if (
        response.is_redirect
        or response.status_code < 200
        or response.status_code >= 300
    ):
        raise EmailDeliveryError(f"email_provider_http_{response.status_code}")
    return response.headers.get("X-Message-Id")


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _retry_delay(attempt: int) -> timedelta:
    return min(timedelta(seconds=60 * (2 ** max(0, attempt - 1))), MAX_BACKOFF)


def _safe_error_code(exc: BaseException) -> str:
    candidate = str(exc).strip().lower().replace(" ", "_")
    if isinstance(exc, EmailNotConfigured):
        return "provider_not_configured"
    if isinstance(exc, EmailDeliveryError) and _SAFE_ERROR.fullmatch(candidate):
        return candidate
    if isinstance(exc, ProviderDeliveryError) and _SAFE_ERROR.fullmatch(candidate):
        return candidate
    return "email_delivery_failed"


def _due_receipt_clause(receipts: Any, now: datetime) -> Any:
    stale_before = now - CLAIM_TIMEOUT
    return or_(
        and_(
            receipts.c.status.in_(["pending", "retry"]),
            or_(
                receipts.c.next_attempt_at.is_(None), receipts.c.next_attempt_at <= now
            ),
        ),
        and_(receipts.c.status == "processing", receipts.c.claimed_at <= stale_before),
    )


async def _eligible_recipients(
    organization_id: str, member_id: str | None
) -> list[dict[str, str]]:
    members = await reflect_table("members")
    conditions = [members.c.organization_id == organization_id]
    if member_id is None:
        conditions.append(members.c.role.in_(["owner", "admin"]))
    else:
        conditions.append(members.c.id == member_id)
    if "status" in members.c:
        conditions.append(members.c.status == "active")
    async with engine.begin() as conn:
        rows = (
            (
                await conn.execute(
                    select(members.c.id, members.c.email).where(*conditions)
                )
            )
            .mappings()
            .all()
        )
    return [
        {"member_id": str(row["id"]), "email": str(row["email"] or "")} for row in rows
    ]


async def _org_admin_emails(organization_id: str) -> list[str]:
    """Compatibility helper retained for invitation and focused delivery tests."""

    return [
        r["email"]
        for r in await _eligible_recipients(organization_id, None)
        if r["email"]
    ]


async def _member_email(member_id: str) -> list[str]:
    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(select(members.c.email).where(members.c.id == member_id))
        ).first()
    return [str(row[0])] if row and row[0] else []


async def _materialize_notification_receipts(
    organization_id: str, *, limit: int
) -> int:
    notifications = await reflect_table("notifications")
    receipts = await reflect_table("notification_delivery_receipts")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        rows = (
            (
                await conn.execute(
                    select(notifications)
                    .where(
                        notifications.c.organization_id == organization_id,
                        notifications.c.emailed_at.is_(None),
                    )
                    .order_by(notifications.c.created_at.asc())
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )

    targeted_member_ids = {
        str(row["member_id"]) for row in rows if row.get("member_id") is not None
    }
    member_conditions = [members.c.organization_id == organization_id]
    if "status" in members.c:
        member_conditions.append(members.c.status == "active")
    recipient_scope = members.c.role.in_(["owner", "admin"])
    if targeted_member_ids:
        recipient_scope = or_(recipient_scope, members.c.id.in_(targeted_member_ids))
    member_conditions.append(recipient_scope)
    async with engine.begin() as conn:
        recipient_rows = (
            (
                await conn.execute(
                    select(members.c.id, members.c.email, members.c.role).where(
                        *member_conditions
                    )
                )
            )
            .mappings()
            .all()
        )
    admin_recipients = [
        {"member_id": str(row["id"]), "email": str(row["email"] or "")}
        for row in recipient_rows
        if row["role"] in {"owner", "admin"}
    ]
    member_recipients = {
        str(row["id"]): {
            "member_id": str(row["id"]),
            "email": str(row["email"] or ""),
        }
        for row in recipient_rows
    }

    values: list[dict[str, Any]] = []
    for row in rows:
        notification = dict(row)
        target_id = notification.get("member_id")
        intended_recipients = (
            admin_recipients
            if target_id is None
            else [member_recipients[str(target_id)]]
            if str(target_id) in member_recipients
            else []
        )
        for recipient in intended_recipients:
            delivery_key = f"notification:{notification['id']}:member:{recipient['member_id']}:email"
            status = "pending" if recipient["email"] else "dead_letter"
            values.append(
                {
                    "organization_id": organization_id,
                    "region": notification.get("region") or settings.region,
                    "notification_id": notification["id"],
                    "member_id": recipient["member_id"],
                    "delivery_kind": "notification",
                    "channel": "email",
                    "dedupe_key": delivery_key,
                    "recipient": recipient["email"],
                    "subject": notification["title"],
                    "body": notification.get("body") or notification["title"],
                    "status": status,
                    "max_attempts": MAX_ATTEMPTS,
                    "last_error_code": None
                    if recipient["email"]
                    else "member_email_missing",
                }
            )
    for value in values:
        _assert_metadata_only(dict(value.get("provider_payload") or {}))
    if not values:
        return 0
    async with engine.begin() as conn:
        result = await conn.execute(
            pg_insert(receipts)
            .values(values)
            .on_conflict_do_nothing(index_elements=["organization_id", "dedupe_key"])
        )
    return max(0, int(result.rowcount or 0))


async def _materialize_channel_notification_receipts(
    organization_id: str, *, channel: str, limit: int
) -> int:
    if channel not in {"slack", "teams"}:
        return 0
    notifications = await reflect_table("notifications")
    receipts = await reflect_table("notification_delivery_receipts")
    bindings = await reflect_table("agent_publication_bindings")
    async with engine.begin() as conn:
        notification_rows = (
            await conn.execute(
                select(notifications)
                .where(notifications.c.organization_id == organization_id, notifications.c.emailed_at.is_(None))
                .order_by(notifications.c.created_at.asc())
                .limit(limit)
            )
        ).mappings().all()
        binding_rows = (
            await conn.execute(
                select(bindings).where(
                    bindings.c.organization_id == organization_id,
                    bindings.c.provider == channel,
                    bindings.c.status == "active",
                    bindings.c.provider_status == "ready",
                )
            )
        ).mappings().all()
    values: list[dict[str, Any]] = []
    for notification in notification_rows:
        target_member = str(notification["member_id"]) if notification.get("member_id") else None
        for binding in binding_rows:
            if target_member and str(binding["member_id"]) != target_member:
                continue
            values.append(
                {
                    "organization_id": organization_id,
                    "region": notification.get("region") or settings.region,
                    "notification_id": notification["id"],
                    "member_id": binding["member_id"],
                    "delivery_kind": "notification",
                    "channel": channel,
                    "dedupe_key": f"notification:{notification['id']}:binding:{binding['id']}:{channel}",
                    "recipient": str(binding["external_channel_id"]),
                    "subject": str(notification["title"]),
                    "body": str(notification.get("body") or notification["title"]),
                    "status": "pending",
                    "max_attempts": MAX_ATTEMPTS,
                    "binding_id": binding["id"],
                    # Metadata-only. The binding carries provider ids and the
                    # body column carries rendered notification text.
                    "provider_payload": {"notification_type": str(notification["type"])},
                }
            )
    for value in values:
        _assert_metadata_only(dict(value.get("provider_payload") or {}))
    if not values:
        return 0
    async with engine.begin() as conn:
        result = await conn.execute(pg_insert(receipts).values(values).on_conflict_do_nothing(index_elements=["organization_id", "dedupe_key"]))
    return max(0, int(result.rowcount or 0))


async def _claim_receipts(
    *,
    organization_id: str | None,
    limit: int,
    now: datetime,
    delivery_kind: str | None = None,
) -> list[dict[str, Any]]:
    receipts = await reflect_table("notification_delivery_receipts")
    claim_token = str(uuid.uuid4())
    stale_before = now - CLAIM_TIMEOUT
    due = _due_receipt_clause(receipts, now)
    scope_conditions: list[Any] = []
    if organization_id:
        scope_conditions.append(receipts.c.organization_id == organization_id)
    if delivery_kind:
        scope_conditions.append(receipts.c.delivery_kind == delivery_kind)
    conditions = [*scope_conditions, due]
    async with engine.begin() as conn:
        # A worker may die after a non-idempotent chat provider accepted the
        # send but before Chronos committed the receipt. Retrying that boundary
        # can duplicate a customer-visible message, so fail closed into the
        # operator-visible dead letter instead of guessing.
        await conn.execute(
            update(receipts)
            .where(
                *scope_conditions,
                or_(
                    receipts.c.channel.in_(["slack", "teams"]),
                    receipts.c.delivery_kind == "agent_response",
                ),
                receipts.c.status == "processing",
                receipts.c.claimed_at <= stale_before,
            )
            .values(
                status="dead_letter",
                last_error_code="ambiguous_provider_outcome",
                claimed_at=None,
                claim_token=None,
                next_attempt_at=None,
                updated_at=now,
            )
        )
        # A claim that exhausted its budget before the worker crashed is an
        # ambiguous provider boundary. Dead-letter it instead of exceeding the
        # configured send-attempt bound.
        await conn.execute(
            update(receipts)
            .where(
                *scope_conditions,
                receipts.c.status == "processing",
                receipts.c.claimed_at <= stale_before,
                receipts.c.attempts >= receipts.c.max_attempts,
            )
            .values(
                status="dead_letter",
                last_error_code="claim_expired_after_max_attempts",
                claimed_at=None,
                claim_token=None,
                next_attempt_at=None,
                updated_at=now,
            )
        )
        ids = (
            (
                await conn.execute(
                    select(receipts.c.id)
                    .where(*conditions)
                    .order_by(
                        receipts.c.next_attempt_at.asc().nullsfirst(),
                        receipts.c.created_at.asc(),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            return []
        await conn.execute(
            update(receipts)
            .where(receipts.c.id.in_(ids))
            .values(
                status="processing",
                attempts=receipts.c.attempts + 1,
                claimed_at=now,
                claim_token=claim_token,
                updated_at=now,
            )
        )
        claimed = (
            (
                await conn.execute(
                    select(receipts).where(receipts.c.claim_token == claim_token)
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in claimed]


async def _finish_receipt(
    receipt: dict[str, Any], *, provider_message_id: str | None, now: datetime
) -> None:
    receipts = await reflect_table("notification_delivery_receipts")
    async with engine.begin() as conn:
        await conn.execute(
            update(receipts)
            .where(
                receipts.c.id == receipt["id"],
                receipts.c.claim_token == receipt["claim_token"],
                receipts.c.status == "processing",
            )
            .values(
                status="delivered",
                delivered_at=now,
                provider_message_id=provider_message_id,
                last_error_code=None,
                next_attempt_at=None,
                claimed_at=None,
                claim_token=None,
                updated_at=now,
            )
        )
        if receipt.get("publication_id"):
            publications = await reflect_table("agent_publications")
            await conn.execute(
                update(publications)
                .where(publications.c.id == receipt["publication_id"], publications.c.organization_id == receipt["organization_id"])
                .values(provider_status="ready", last_outbound_at=now, last_error_code=None, updated_at=now)
            )


async def _fail_receipt(
    receipt: dict[str, Any], *, error_code: str, now: datetime, force_dead_letter: bool = False
) -> str:
    receipts = await reflect_table("notification_delivery_receipts")
    attempts = int(receipt.get("attempts") or 0)
    max_attempts = int(receipt.get("max_attempts") or MAX_ATTEMPTS)
    status = "dead_letter" if force_dead_letter or attempts >= max_attempts else "retry"
    next_attempt = None if status == "dead_letter" else now + _retry_delay(attempts)
    async with engine.begin() as conn:
        await conn.execute(
            update(receipts)
            .where(
                receipts.c.id == receipt["id"],
                receipts.c.claim_token == receipt["claim_token"],
                receipts.c.status == "processing",
            )
            .values(
                status=status,
                next_attempt_at=next_attempt,
                last_error_code=error_code,
                claimed_at=None,
                claim_token=None,
                updated_at=now,
            )
        )
        if receipt.get("publication_id"):
            publications = await reflect_table("agent_publications")
            await conn.execute(
                update(publications)
                .where(publications.c.id == receipt["publication_id"], publications.c.organization_id == receipt["organization_id"])
                .values(provider_status="degraded", last_error_code=error_code, updated_at=now)
            )
    return status


async def _mark_completed_notifications(
    organization_id: str, notification_ids: set[str]
) -> None:
    if not notification_ids:
        return
    notifications = await reflect_table("notifications")
    receipts = await reflect_table("notification_delivery_receipts")
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(receipts.c.notification_id, receipts.c.status).where(
                    receipts.c.organization_id == organization_id,
                    receipts.c.notification_id.in_(notification_ids),
                    receipts.c.delivery_kind == "notification",
                )
            )
        ).all()
        statuses_by_notification: dict[str, list[str]] = {}
        for notification_id, status in rows:
            statuses_by_notification.setdefault(str(notification_id), []).append(
                str(status)
            )
        completed_ids = [
            notification_id
            for notification_id, statuses in statuses_by_notification.items()
            if statuses and all(status == "delivered" for status in statuses)
        ]
        if completed_ids:
            await conn.execute(
                update(notifications)
                .where(
                    notifications.c.organization_id == organization_id,
                    notifications.c.id.in_(completed_ids),
                    notifications.c.emailed_at.is_(None),
                )
                .values(emailed_at=now)
            )


async def _dispatch_claimed(
    claimed: list[dict[str, Any]], *, now: datetime
) -> dict[str, int]:
    delivered = retried = dead_letter = 0
    completed_by_org: dict[str, set[str]] = {}
    for receipt in claimed:
        try:
            if receipt["channel"] == "email":
                message_id = await asyncio.to_thread(
                    _provider_send_email,
                    to=receipt["recipient"],
                    subject=receipt["subject"],
                    body=receipt["body"],
                    delivery_key=receipt["dedupe_key"],
                )
            else:
                message_id = await _provider_send_channel(receipt)
        except AmbiguousProviderDelivery as exc:
            terminal = await _fail_receipt(
                receipt,
                error_code=_safe_error_code(exc),
                now=now,
                force_dead_letter=True,
            )
            dead_letter += terminal == "dead_letter"
            continue
        except (EmailNotConfigured, EmailDeliveryError, ProviderDeliveryError) as exc:
            terminal = await _fail_receipt(
                receipt, error_code=_safe_error_code(exc), now=now
            )
            retried += terminal == "retry"
            dead_letter += terminal == "dead_letter"
            continue
        except Exception as exc:  # provider adapters must not abort the batch
            terminal = await _fail_receipt(
                receipt, error_code=_safe_error_code(exc), now=now
            )
            retried += terminal == "retry"
            dead_letter += terminal == "dead_letter"
            continue
        await _finish_receipt(receipt, provider_message_id=message_id, now=now)
        delivered += 1
        if receipt.get("notification_id"):
            completed_by_org.setdefault(receipt["organization_id"], set()).add(
                str(receipt["notification_id"])
            )
    for org_id, notification_ids in completed_by_org.items():
        await _mark_completed_notifications(org_id, notification_ids)
    return {"delivered": delivered, "retried": retried, "dead_letter": dead_letter}


async def _provider_send_channel(receipt: dict[str, Any]) -> str | None:
    """Send Slack/Teams using the tenant-bound connector on the receipt.

    `provider_payload` is deliberately metadata-only. Message text lives in the
    existing encrypted-at-rest receipt body column and never appears in audit.
    """

    bindings = await reflect_table("agent_publication_bindings")
    connectors = await reflect_table("connectors")
    async with engine.begin() as conn:
        binding = (
            await conn.execute(
                select(bindings).where(
                    bindings.c.id == receipt.get("binding_id"),
                    bindings.c.organization_id == receipt["organization_id"],
                    bindings.c.status == "active",
                    bindings.c.provider == receipt["channel"],
                )
            )
        ).mappings().first()
        connector = None
        if binding:
            connector = (
                await conn.execute(
                    select(connectors).where(
                        connectors.c.id == binding["connector_id"],
                        connectors.c.organization_id == receipt["organization_id"],
                        connectors.c.provider == receipt["channel"],
                        connectors.c.status == "active",
                    )
                )
            ).mappings().first()
    if not binding or not connector:
        raise ProviderDeliveryError("provider_binding_unavailable")
    vault_ref = str(connector["vault_ref"] or "")
    metadata = dict(receipt.get("provider_payload") or {})
    if vault_ref.startswith("composio:"):
        parsed = composio_client.parse_managed_vault_ref(vault_ref)
        if parsed is None:
            raise ProviderDeliveryError("managed_connector_reference_invalid")
        _provider, entity = parsed
        try:
            if receipt["channel"] == "slack":
                result = await composio_client.execute_action(
                    "SLACK_CHAT_POST_MESSAGE",
                    {
                        "channel": str(binding["external_channel_id"]),
                        "text": str(receipt["body"]),
                        "client_msg_id": str(receipt["dedupe_key"]),
                        **({"thread_ts": metadata["thread_id"]} if metadata.get("thread_id") else {}),
                    },
                    entity=entity,
                )
            else:
                result = await composio_client.execute_action(
                    "MICROSOFT_TEAMS_SEND_MESSAGE_TO_CHANNEL",
                    {
                        "team_id": str(binding["external_tenant_id"]),
                        "channel_id": str(binding["external_channel_id"]),
                        "content": str(receipt["body"]),
                    },
                    entity=entity,
                )
        except Exception as exc:
            # The SDK does not expose whether an exception happened before or
            # after provider acceptance. Retrying could duplicate a reply.
            raise AmbiguousProviderDelivery("ambiguous_provider_outcome") from exc
        if isinstance(result, dict):
            value = result.get("id") or result.get("message_id") or (result.get("data") or {}).get("ts")
            return str(value) if value else None
        return None
    try:
        if receipt["channel"] == "slack":
            result = await generic_http.call(
                vault_ref,
                "POST",
                "/chat.postMessage",
                org_id=str(receipt["organization_id"]),
                body={
                    "channel": str(binding["external_channel_id"]),
                    "text": str(receipt["body"]),
                    "client_msg_id": str(receipt["dedupe_key"]),
                    **({"thread_ts": metadata["thread_id"]} if metadata.get("thread_id") else {}),
                },
            )
            if result.get("ok") is False:
                raise ProviderDeliveryError("slack_provider_rejected")
            return str(result.get("ts") or "") or None
        result = await generic_http.call(
            vault_ref,
            "POST",
            f"/teams/{binding['external_tenant_id']}/channels/{binding['external_channel_id']}/messages",
            org_id=str(receipt["organization_id"]),
            body={"body": {"contentType": "text", "content": str(receipt["body"])}},
        )
        return str(result.get("id") or "") or None
    except ProviderDeliveryError:
        raise
    except generic_http.AmbiguousProviderMutation as exc:
        raise AmbiguousProviderDelivery("ambiguous_provider_outcome") from exc
    except Exception as exc:
        raise ProviderDeliveryError("provider_send_failed") from exc


async def enqueue_agent_response(task_id: str, answer: str) -> bool:
    """Materialize one policy-bound response receipt for a publication task.

    External replies default to human approval. Web/API responses use the same
    receipt as a release gate even though they are fetched rather than pushed.
    """

    tasks = await reflect_table("tasks")
    publications = await reflect_table("agent_publications")
    receipts = await reflect_table("notification_delivery_receipts")
    approvals = await reflect_table("approvals")
    async with engine.begin() as conn:
        task = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
    if not task:
        return False
    publication_state = dict(task.get("agent_state") or {}).get("agent_publication") or {}
    publication_id = publication_state.get("id")
    if not publication_id:
        return False
    async with engine.begin() as conn:
        publication = (
            await conn.execute(
                select(publications).where(
                    publications.c.id == publication_id,
                    publications.c.organization_id == task["organization_id"],
                )
            )
        ).mappings().first()
    if not publication or publication["target"] not in {"email", "slack", "teams", "web", "api"}:
        return False
    policy_value = (publication.get("approval_policy") or {}).get(
        "external_replies", "require_approval"
    )
    approval_required = not (
        policy_value is False
        or str(policy_value).strip().lower()
        in {"allow", "allowed", "always_allow", "automatic", "auto"}
    )
    active = publication["status"] == "active"
    push_channel = publication["target"] in {"email", "slack", "teams"}
    status = (
        "dead_letter"
        if not active
        else "approval_pending"
        if approval_required
        else "pending"
        if push_channel
        else "delivered"
    )
    # Metadata-only: provider ids and stable hashes, never bodies, addresses,
    # secrets, or arbitrary webhook payloads.
    provider_payload = {
        "external_message_id_hash": hashlib.sha256(str(publication_state.get("external_message_id") or "").encode()).hexdigest(),
    }
    if publication["target"] in {"slack", "teams"}:
        provider_payload["thread_id"] = str(
            publication_state.get("external_conversation_id") or ""
        )[:240]
    _assert_metadata_only(provider_payload)
    recipient = str(publication.get("external_channel_id") or publication_id)
    subject = f"Response from {publication['display_name']}"
    if publication["target"] == "email":
        recipient = str(publication_state.get("reply_to_email") or recipient)
        original_subject = str(publication_state.get("email_subject") or "").strip()
        if original_subject:
            subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"
    value = {
        "organization_id": str(task["organization_id"]),
        "region": task.get("region") or settings.region,
        "notification_id": None,
        "member_id": str(publication["created_by"]),
        "delivery_kind": "agent_response",
        "channel": str(publication["target"]),
        "dedupe_key": f"agent_response:{publication_id}:task:{task_id}",
        "recipient": recipient,
        "subject": subject[:500],
        "body": answer[:50_000],
        "status": status,
        "max_attempts": MAX_ATTEMPTS,
        "last_error_code": None if status == "pending" else "publication_inactive",
        "publication_id": publication_id,
        "binding_id": publication.get("binding_id"),
        "task_id": task_id,
        "external_conversation_id": str(publication_state.get("external_conversation_id") or "")[:500],
        "provider_payload": provider_payload,
        "delivered_at": datetime.now(timezone.utc) if status == "delivered" else None,
    }
    async with engine.begin() as conn:
        receipt_id = (
            await conn.execute(
                pg_insert(receipts)
                .values(value)
                .on_conflict_do_nothing(index_elements=["organization_id", "dedupe_key"])
                .returning(receipts.c.id)
            )
        ).scalar_one_or_none()
        if receipt_id is None:
            return False
        if status == "delivered":
            await conn.execute(
                update(publications)
                .where(
                    publications.c.id == publication_id,
                    publications.c.organization_id == task["organization_id"],
                )
                .values(
                    provider_status="ready",
                    last_outbound_at=datetime.now(timezone.utc),
                    last_error_code=None,
                    updated_at=datetime.now(timezone.utc),
                )
            )
        approval_id: str | None = None
        if status == "approval_pending":
            approval_id = str(
                (
                    await conn.execute(
                        insert(approvals)
                        .values(
                            organization_id=str(task["organization_id"]),
                            region=task.get("region") or settings.region,
                            task_id=task_id,
                            step_id="agent_publication_reply",
                            action_type="agent.publication.reply",
                            action_payload={
                                "receipt_id": str(receipt_id),
                                "publication_id": str(publication_id),
                                "target": str(publication["target"]),
                                "recipient": recipient,
                                "subject": subject[:500],
                                "body": answer[:50_000],
                                "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
                            },
                            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                        )
                        .returning(approvals.c.id)
                    )
                ).scalar_one()
            )
            await conn.execute(
                update(receipts)
                .where(
                    receipts.c.id == receipt_id,
                    receipts.c.organization_id == task["organization_id"],
                    receipts.c.status == "approval_pending",
                )
                .values(approval_id=approval_id, updated_at=datetime.now(timezone.utc))
            )
    if approval_id:
        try:
            from core import notifications

            await notifications.emit(
                organization_id=str(task["organization_id"]),
                type="approval_request",
                title=f"Approval needed: publish {publication['target']} reply",
                body=f"A response from {publication['display_name']} is waiting for approval.",
                severity="warning",
                resource_type="task",
                resource_id=task_id,
                created_by="chronos",
            )
        except Exception:
            pass
    return True


async def _receipt_counts(
    organization_id: str, *, delivery_kind: str | None = None
) -> dict[str, int]:
    receipts = await reflect_table("notification_delivery_receipts")
    conditions = [receipts.c.organization_id == organization_id]
    if delivery_kind:
        conditions.append(receipts.c.delivery_kind == delivery_kind)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(receipts.c.status, func.count())
                .where(*conditions)
                .group_by(receipts.c.status)
            )
        ).all()
    counts = {str(status): int(count) for status, count in rows}
    return {
        "pending": sum(
            counts.get(state, 0) for state in ("pending", "retry", "processing")
        ),
        "dead_letter": counts.get("dead_letter", 0),
    }


async def _pending_notification_count(organization_id: str) -> int:
    notifications = await reflect_table("notifications")
    async with engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(notifications)
                    .where(
                        notifications.c.organization_id == organization_id,
                        notifications.c.emailed_at.is_(None),
                    )
                )
            ).scalar()
            or 0
        )


async def deliver_pending(
    organization_id: str, *, limit: int = 100, now: datetime | None = None
) -> dict[str, Any]:
    """Materialize and dispatch one bounded, tenant-scoped provider batch."""

    notif_settings = await _notification_settings(organization_id)
    enabled_channels = [channel for channel in ("email", "slack", "teams") if notif_settings.get(channel) is True]
    if not enabled_channels:
        return {"status": "skipped", "reason": "external_notifications_disabled_for_org", "delivered": 0}

    materialized = 0
    if "email" in enabled_channels:
        materialized += await _materialize_notification_receipts(organization_id, limit=limit)
    for channel in ("slack", "teams"):
        if channel in enabled_channels:
            materialized += await _materialize_channel_notification_receipts(organization_id, channel=channel, limit=limit)

    claim_now = _utc(now)
    claimed = await _claim_receipts(
        organization_id=organization_id,
        limit=limit,
        now=claim_now,
        delivery_kind="notification",
    )
    outcome = await _dispatch_claimed(claimed, now=claim_now)
    counts = await _receipt_counts(organization_id, delivery_kind="notification")
    if outcome["delivered"]:
        await audit.log(
            "notification",
            "system",
            "notification_email_sent",
            organization_id=organization_id,
            resource_type="notification",
            payload={"delivered": outcome["delivered"]},
        )
    if outcome["dead_letter"]:
        await audit.log(
            "notification",
            "system",
            "notification_email_dead_lettered",
            organization_id=organization_id,
            resource_type="notification",
            payload={"dead_lettered": outcome["dead_letter"]},
        )
    degraded = "email" in enabled_channels and not email_is_configured()
    return {
        "status": "degraded" if degraded else "ok",
        **({"reason": "email_provider_not_configured"} if degraded else {}),
        **outcome,
        "pending": counts["pending"],
        "dead_letter_total": counts["dead_letter"],
        "materialized": materialized,
    }


async def build_digest(
    organization_id: str,
    member_id: str,
    *,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> dict[str, Any]:
    """Build an unread digest, optionally bounded to a UTC delivery window."""

    notifications = await reflect_table("notifications")
    read_receipts = await reflect_table("notification_receipts")
    joined = notifications.outerjoin(
        read_receipts,
        and_(
            read_receipts.c.organization_id == notifications.c.organization_id,
            read_receipts.c.notification_id == notifications.c.id,
            read_receipts.c.member_id == member_id,
        ),
    )
    conditions = [
        notifications.c.organization_id == organization_id,
        or_(
            notifications.c.member_id.is_(None), notifications.c.member_id == member_id
        ),
        read_receipts.c.read_at.is_(None),
        read_receipts.c.dismissed_at.is_(None),
    ]
    if period_start:
        conditions.append(notifications.c.created_at >= _utc(period_start))
    if period_end:
        conditions.append(notifications.c.created_at < _utc(period_end))
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(notifications.c.type, func.count())
                .select_from(joined)
                .where(*conditions)
                .group_by(notifications.c.type)
            )
        ).all()
    by_type = {str(ntype): int(count) for ntype, count in rows}
    return {
        "organization_id": organization_id,
        "member_id": member_id,
        "unread_total": sum(by_type.values()),
        "by_type": by_type,
        "period_start": _utc(period_start).isoformat() if period_start else None,
        "period_end": _utc(period_end).isoformat() if period_end else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _weekly_window(now: datetime) -> tuple[datetime, datetime]:
    now = _utc(now)
    current_week = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return current_week - timedelta(days=7), current_week


def _digest_body(digest: dict[str, Any]) -> str:
    lines = [
        "Your Chronos weekly notification digest",
        "",
        f"Unread notifications: {digest['unread_total']}",
    ]
    for notification_type, count in sorted(digest["by_type"].items()):
        lines.append(f"- {notification_type.replace('_', ' ').title()}: {count}")
    lines.extend(["", "Open Chronos to review and manage these notifications."])
    return "\n".join(lines)


async def _email_enabled_org_ids(*, require_weekly: bool = False) -> set[str]:
    settings_documents = await reflect_table("settings_documents")
    async with engine.begin() as conn:
        rows = (
            (
                await conn.execute(
                    select(
                        settings_documents.c.organization_id,
                        settings_documents.c["values"],
                    ).where(
                        settings_documents.c.scope == "org",
                        settings_documents.c.scope_id
                        == settings_documents.c.organization_id,
                        settings_documents.c.section == "notifications",
                    )
                )
            )
            .mappings()
            .all()
        )
    enabled: set[str] = set()
    for row in rows:
        values = dict(row["values"] or {})
        if values.get("email") is not True:
            continue
        if require_weekly and values.get("weekly_digest") is not True:
            continue
        enabled.add(str(row["organization_id"]))
    return enabled


async def _external_delivery_enabled_org_ids() -> set[str]:
    settings_documents = await reflect_table("settings_documents")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(settings_documents.c.organization_id, settings_documents.c["values"])
                .where(
                    settings_documents.c.scope == "org",
                    settings_documents.c.scope_id == settings_documents.c.organization_id,
                    settings_documents.c.section == "notifications",
                )
            )
        ).mappings().all()
    return {
        str(row["organization_id"])
        for row in rows
        if any((row["values"] or {}).get(channel) is True for channel in ("email", "slack", "teams"))
    }


async def materialize_weekly_digests(
    *, now: datetime | None = None, limit: int = 500
) -> int:
    """Create at most one receipt per member for the completed ISO week."""

    members = await reflect_table("members")
    receipts = await reflect_table("notification_delivery_receipts")
    period_start, period_end = _weekly_window(_utc(now))
    # Both flags default false; only explicitly opted-in organizations are due.
    # Querying the small settings set avoids starving a tenant merely because
    # its organization id sorts after an arbitrary scan limit.
    org_ids = await _email_enabled_org_ids(require_weekly=True)

    values: list[dict[str, Any]] = []
    for org_id in sorted(org_ids):
        member_conditions = [members.c.organization_id == org_id]
        if "status" in members.c:
            member_conditions.append(members.c.status == "active")
        async with engine.begin() as conn:
            member_rows = (
                (
                    await conn.execute(
                        select(members.c.id, members.c.email).where(*member_conditions)
                    )
                )
                .mappings()
                .all()
            )
        for member in member_rows:
            if not member["email"]:
                continue
            digest = await build_digest(
                str(org_id),
                str(member["id"]),
                period_start=period_start,
                period_end=period_end,
            )
            if digest["unread_total"] == 0:
                continue
            values.append(
                {
                    "organization_id": str(org_id),
                    "region": settings.region,
                    "notification_id": None,
                    "member_id": str(member["id"]),
                    "delivery_kind": "weekly_digest",
                    "channel": "email",
                    "dedupe_key": f"weekly_digest:{member['id']}:{period_start.date().isoformat()}",
                    "recipient": str(member["email"]),
                    "subject": "Your weekly Chronos digest",
                    "body": _digest_body(digest),
                    "status": "pending",
                    "max_attempts": MAX_ATTEMPTS,
                    "period_start": period_start,
                    "period_end": period_end,
                }
            )
            if len(values) >= limit:
                break
        if len(values) >= limit:
            break
    if not values:
        return 0
    async with engine.begin() as conn:
        result = await conn.execute(
            pg_insert(receipts)
            .values(values)
            .on_conflict_do_nothing(index_elements=["organization_id", "dedupe_key"])
        )
    return max(0, int(result.rowcount or 0))


async def run_delivery_cycle(
    *, max_orgs: int = 25, per_org_limit: int = 50
) -> dict[str, int]:
    """Process durable notification and published-agent delivery receipts."""

    totals = {"organizations": 0, "delivered": 0, "retried": 0, "dead_letter": 0}
    notifications = await reflect_table("notifications")
    receipts = await reflect_table("notification_delivery_receipts")
    enabled_orgs = await _external_delivery_enabled_org_ids()
    weekly_enabled_orgs = await _email_enabled_org_ids(require_weekly=True) if email_is_configured() else set()
    cycle_now = datetime.now(timezone.utc)
    notification_receipts = receipts.alias("notification_receipts_for_candidate")
    unmaterialized = notifications.outerjoin(
        notification_receipts,
        and_(
            notification_receipts.c.organization_id == notifications.c.organization_id,
            notification_receipts.c.notification_id == notifications.c.id,
            notification_receipts.c.delivery_kind == "notification",
        ),
    )
    async with engine.begin() as conn:
        receipt_orgs = (
            select(
                receipts.c.organization_id,
                func.min(
                    func.coalesce(receipts.c.next_attempt_at, receipts.c.created_at)
                ).label("oldest"),
            )
            .where(
                receipts.c.delivery_kind == "notification",
                _due_receipt_clause(receipts, cycle_now),
            )
            .group_by(receipts.c.organization_id)
        )
        notification_orgs = (
            select(
                notifications.c.organization_id,
                func.min(notifications.c.created_at).label("oldest"),
            )
            .select_from(unmaterialized)
            .where(notifications.c.emailed_at.is_(None))
            .where(notification_receipts.c.id.is_(None))
            .group_by(notifications.c.organization_id)
        )
        candidates = union_all(receipt_orgs, notification_orgs).subquery()
        rows = (
            await conn.execute(
                select(
                    candidates.c.organization_id,
                    func.min(candidates.c.oldest).label("oldest"),
                )
                .group_by(candidates.c.organization_id)
                .order_by(func.min(candidates.c.oldest).asc())
            )
        ).all()
        weekly_rows = (
            await conn.execute(
                select(
                    receipts.c.organization_id,
                    func.min(
                        func.coalesce(receipts.c.next_attempt_at, receipts.c.created_at)
                    ).label("oldest"),
                )
                .where(
                    receipts.c.delivery_kind == "weekly_digest",
                    _due_receipt_clause(receipts, cycle_now),
                )
                .group_by(receipts.c.organization_id)
                .order_by(
                    func.min(
                        func.coalesce(receipts.c.next_attempt_at, receipts.c.created_at)
                    ).asc()
                )
            )
        ).all()
        response_rows = (
            await conn.execute(
                select(
                    receipts.c.organization_id,
                    func.min(func.coalesce(receipts.c.next_attempt_at, receipts.c.created_at)).label("oldest"),
                )
                .where(receipts.c.delivery_kind == "agent_response", _due_receipt_clause(receipts, cycle_now))
                .group_by(receipts.c.organization_id)
                .order_by(func.min(func.coalesce(receipts.c.next_attempt_at, receipts.c.created_at)).asc())
                .limit(max_orgs)
            )
        ).all()
    org_ids = [str(org_id) for org_id, _oldest in rows if str(org_id) in enabled_orgs][
        :max_orgs
    ]
    processed_orgs: set[str] = set()
    for org_id, _oldest in response_rows:
        try:
            claimed = await _claim_receipts(
                organization_id=str(org_id),
                limit=per_org_limit,
                now=cycle_now,
                delivery_kind="agent_response",
            )
            outcome = await _dispatch_claimed(claimed, now=cycle_now)
        except Exception:
            log.exception("agent publication delivery cycle failed for org=%s", org_id)
            continue
        processed_orgs.add(str(org_id))
        for key in ("delivered", "retried", "dead_letter"):
            totals[key] += int(outcome.get(key, 0))
    for org_id in org_ids:
        try:
            result = await deliver_pending(str(org_id), limit=per_org_limit)
        except Exception:
            # One damaged tenant or transient DB/provider problem cannot starve
            # every other tenant in the scheduler batch.
            log.exception("notification delivery cycle failed for org=%s", org_id)
            continue
        processed_orgs.add(org_id)
        for key in ("delivered", "retried", "dead_letter"):
            totals[key] += int(result.get(key, 0))
    weekly_org_ids = [
        str(org_id)
        for org_id, _oldest in weekly_rows
        if str(org_id) in weekly_enabled_orgs
    ][:max_orgs]
    for org_id in weekly_org_ids:
        try:
            claimed = await _claim_receipts(
                organization_id=org_id,
                limit=per_org_limit,
                now=cycle_now,
                delivery_kind="weekly_digest",
            )
            outcome = await _dispatch_claimed(claimed, now=cycle_now)
        except Exception:
            log.exception("weekly digest retry cycle failed for org=%s", org_id)
            continue
        processed_orgs.add(org_id)
        for key in ("delivered", "retried", "dead_letter"):
            totals[key] += int(outcome.get(key, 0))
    totals["organizations"] = len(processed_orgs)
    return totals


async def run_weekly_digest_cycle(
    *, now: datetime | None = None, limit: int = 500
) -> dict[str, int]:
    """Materialize one completed-week digest per eligible member and dispatch."""

    materialized = await materialize_weekly_digests(now=now, limit=limit)
    claim_now = _utc(now)
    if not email_is_configured():
        return {
            "materialized": materialized,
            "delivered": 0,
            "retried": 0,
            "dead_letter": 0,
        }
    claimed: list[dict[str, Any]] = []
    for org_id in sorted(await _email_enabled_org_ids(require_weekly=True)):
        remaining = limit - len(claimed)
        if remaining <= 0:
            break
        claimed.extend(
            await _claim_receipts(
                organization_id=org_id,
                limit=remaining,
                now=claim_now,
                delivery_kind="weekly_digest",
            )
        )
    return {
        "materialized": materialized,
        **await _dispatch_claimed(claimed, now=claim_now),
    }
