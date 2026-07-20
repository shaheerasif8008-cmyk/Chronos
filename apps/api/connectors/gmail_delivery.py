"""Approval-bound, draft-first Gmail delivery.

The Gmail API does not offer an idempotency key for ``drafts.send``.  A
post-response Redis cache therefore cannot guarantee that a retry following a
worker crash will not send twice.  This module closes that gap by storing the
provider draft/message identifiers in the already-durable approval payload
*before* sending.  A retry either:

* replays a completed result;
* sends the still-existing draft; or
* verifies that the draft's message is already in ``SENT`` and records a
  recovered success.

If provider evidence is ambiguous, delivery stops rather than risk a duplicate.
No email addresses, subject, or body are added to the delivery record.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update

from core.db import engine, reflect_table
from core.exceptions import ApprovalRequired, SafetyLimitViolation
from core.models import ToolResult

MAX_RECIPIENTS = 10
MAX_BODY_BYTES = 100_000
MAX_SUBJECT_CHARS = 998
_LEASE_SECONDS = 120
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


class GmailDeliveryInProgress(RuntimeError):
    """Another worker currently owns the same approved delivery."""


class GmailDeliveryIndeterminate(RuntimeError):
    """Provider state cannot prove whether a draft was sent."""


@dataclass(frozen=True)
class EmailEnvelope:
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body: str
    is_html: bool = False

    @property
    def payload_sha256(self) -> str:
        canonical = json.dumps(
            {
                "to": self.to,
                "cc": self.cc,
                "bcc": self.bcc,
                "subject": self.subject,
                "body": self.body,
                "is_html": self.is_html,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DraftEvidence:
    draft_id: str
    message_id: str | None = None


@dataclass(frozen=True)
class SentEvidence:
    message_id: str
    thread_id: str | None = None


@dataclass(frozen=True)
class DeliveryContext:
    approval_id: str
    organization_id: str
    member_id: str
    task_id: str
    credential_scope: str
    idempotency_key: str

    @property
    def idempotency_sha256(self) -> str:
        return hashlib.sha256(self.idempotency_key.encode("utf-8")).hexdigest()

    @property
    def credential_scope_sha256(self) -> str:
        return hashlib.sha256(self.credential_scope.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DeliveryClaim:
    lease_id: str | None
    state: dict[str, Any]
    replayed: bool = False


def _recipient_values(value: Any, field: str) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    raw_values = value if isinstance(value, (list, tuple)) else [value]
    if not all(isinstance(item, str) for item in raw_values):
        raise SafetyLimitViolation(f"gmail.send: {field} recipients must be strings")
    if any("\r" in item or "\n" in item for item in raw_values):
        raise SafetyLimitViolation(f"gmail.send: {field} contains a header newline")

    parsed = getaddresses(list(raw_values))
    addresses: list[str] = []
    for display_name, address in parsed:
        normalized = address.strip().lower()
        if not normalized or len(normalized) > 254 or not _EMAIL_RE.fullmatch(normalized):
            shown = display_name or address or "empty recipient"
            raise SafetyLimitViolation(f"gmail.send: invalid {field} recipient {shown!r}")
        if normalized not in addresses:
            addresses.append(normalized)
    return tuple(addresses)


def validate_email_args(args: dict[str, Any]) -> EmailEnvelope:
    """Validate and normalize the approved payload before any provider call."""
    to = _recipient_values(args.get("to"), "to")
    cc = _recipient_values(args.get("cc"), "cc")
    bcc = _recipient_values(args.get("bcc"), "bcc")
    if not to:
        raise SafetyLimitViolation("gmail.send: at least one to recipient is required")
    recipient_count = len({*to, *cc, *bcc})
    if recipient_count > MAX_RECIPIENTS:
        raise SafetyLimitViolation(
            f"gmail.send: {recipient_count} recipients exceeds limit of {MAX_RECIPIENTS}"
        )

    subject = args.get("subject", "")
    body = args.get("body", "")
    if not isinstance(subject, str) or not isinstance(body, str):
        raise SafetyLimitViolation("gmail.send: subject and body must be strings")
    if "\r" in subject or "\n" in subject:
        raise SafetyLimitViolation("gmail.send: subject contains a header newline")
    if len(subject) > MAX_SUBJECT_CHARS:
        raise SafetyLimitViolation(
            f"gmail.send: subject exceeds {MAX_SUBJECT_CHARS} characters"
        )
    body_bytes = len(body.encode("utf-8"))
    if body_bytes == 0:
        raise SafetyLimitViolation("gmail.send: body must not be empty")
    if body_bytes > MAX_BODY_BYTES:
        raise SafetyLimitViolation(
            f"gmail.send: body exceeds {MAX_BODY_BYTES} UTF-8 bytes"
        )
    return EmailEnvelope(
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=body,
        is_html=bool(args.get("is_html", False)),
    )


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_result(state: dict[str, Any], *, replayed: bool) -> ToolResult:
    message_id = str(state.get("message_id") or "")
    data = {
        "status": "sent",
        "message_id": message_id,
        "draft_id": str(state.get("draft_id") or ""),
        "thread_id": state.get("thread_id"),
        "idempotency_evidence": str(state.get("idempotency_sha256") or ""),
        "replayed": replayed,
        "recovered_from_provider": bool(state.get("recovered_from_provider", False)),
    }
    return ToolResult(data=data, summary=f"Gmail message sent: {message_id}")


class ApprovalDeliveryStore:
    """Durable state store backed by ``approvals.action_payload``.

    The row is also the proof that the action was approved.  Every claim is
    tenant/task/member scoped and uses a short lease so concurrent task resumes
    cannot both enter the provider send call.
    """

    async def claim(self, context: DeliveryContext, envelope: EmailEnvelope) -> DeliveryClaim:
        approvals = await reflect_table("approvals")
        tasks = await reflect_table("tasks")
        now = datetime.now(timezone.utc)
        lease_id = uuid.uuid4().hex
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(approvals, tasks.c.triggered_by_member_id.label("task_member_id"))
                    .join(tasks, tasks.c.id == approvals.c.task_id)
                    .where(
                        approvals.c.id == context.approval_id,
                        approvals.c.organization_id == context.organization_id,
                        approvals.c.task_id == context.task_id,
                        approvals.c.action_type == "gmail.send",
                        tasks.c.organization_id == context.organization_id,
                    )
                    .with_for_update()
                )
            ).mappings().first()
            if not row:
                raise ApprovalRequired("gmail.send", "matching tenant-scoped approval record was not found")
            record = dict(row)
            if record.get("status") != "approved" or not record.get("decided_by"):
                raise ApprovalRequired("gmail.send", "approval record is not approved")
            decided_at = _parse_time(record.get("decided_at"))
            expires_at = _parse_time(record.get("expires_at"))
            if not decided_at or (expires_at and decided_at > expires_at):
                raise ApprovalRequired("gmail.send", "approval record was decided after expiry")
            if str(record.get("task_member_id") or "") != context.member_id:
                raise ApprovalRequired("gmail.send", "approval is not bound to the credential-owning member")

            payload = dict(record.get("action_payload") or {})
            approved_args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
            if validate_email_args(approved_args).payload_sha256 != envelope.payload_sha256:
                raise ApprovalRequired("gmail.send", "execution payload differs from the approved email")

            state = dict(payload.get("gmail_delivery") or {})
            expected_scope = {
                "organization_id": context.organization_id,
                "member_id": context.member_id,
                "credential_scope_sha256": context.credential_scope_sha256,
                "idempotency_sha256": context.idempotency_sha256,
                "payload_sha256": envelope.payload_sha256,
            }
            if state:
                if any(str(state.get(key) or "") != value for key, value in expected_scope.items()):
                    raise SafetyLimitViolation(
                        "gmail.send: idempotency key was reused for a different tenant, member, credential, or payload"
                    )
                if state.get("status") == "sent":
                    return DeliveryClaim(lease_id=None, state=state, replayed=True)
                lease_expires_at = _parse_time(state.get("lease_expires_at"))
                if state.get("lease_id") and lease_expires_at and lease_expires_at > now:
                    raise GmailDeliveryInProgress("gmail.send is already executing for this approval")
            else:
                state = dict(expected_scope)

            state.update(
                {
                    "status": state.get("status") if state.get("draft_id") else "claimed",
                    "lease_id": lease_id,
                    "lease_expires_at": (now + timedelta(seconds=_LEASE_SECONDS)).isoformat(),
                    "updated_at": now.isoformat(),
                }
            )
            payload["gmail_delivery"] = state
            await conn.execute(
                update(approvals)
                .where(
                    approvals.c.id == context.approval_id,
                    approvals.c.organization_id == context.organization_id,
                    approvals.c.status == "approved",
                )
                .values(action_payload=payload)
            )
        return DeliveryClaim(lease_id=lease_id, state=state)

    async def update_state(
        self,
        context: DeliveryContext,
        lease_id: str,
        changes: dict[str, Any],
        *,
        release: bool = False,
    ) -> dict[str, Any]:
        approvals = await reflect_table("approvals")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(approvals)
                    .where(
                        approvals.c.id == context.approval_id,
                        approvals.c.organization_id == context.organization_id,
                        approvals.c.status == "approved",
                    )
                    .with_for_update()
                )
            ).mappings().first()
            if not row:
                raise ApprovalRequired("gmail.send", "approval disappeared during delivery")
            payload = dict(row.get("action_payload") or {})
            state = dict(payload.get("gmail_delivery") or {})
            if state.get("lease_id") != lease_id:
                raise GmailDeliveryInProgress("gmail.send delivery lease changed")
            state.update(changes)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            if release:
                state.pop("lease_id", None)
                state.pop("lease_expires_at", None)
            payload["gmail_delivery"] = state
            await conn.execute(
                update(approvals)
                .where(
                    approvals.c.id == context.approval_id,
                    approvals.c.organization_id == context.organization_id,
                    approvals.c.status == "approved",
                )
                .values(action_payload=payload)
            )
        return state


CreateDraft = Callable[[EmailEnvelope, str], Awaitable[DraftEvidence]]
InspectDelivery = Callable[[DraftEvidence], Awaitable[SentEvidence | None | bool]]
SendDraft = Callable[[DraftEvidence], Awaitable[SentEvidence]]


async def deliver_approved_email(
    *,
    context: DeliveryContext,
    envelope: EmailEnvelope,
    create_draft: CreateDraft,
    inspect_delivery: InspectDelivery,
    send_draft: SendDraft,
    store: ApprovalDeliveryStore | None = None,
) -> ToolResult:
    """Create a provider draft and send it once for one approved action.

    ``inspect_delivery`` returns ``False`` while the draft still exists,
    ``SentEvidence`` when provider state proves it was sent, and ``None`` when
    the outcome cannot be established safely.
    """
    state_store = store or ApprovalDeliveryStore()
    claim = await state_store.claim(context, envelope)
    if claim.replayed:
        return _safe_result(claim.state, replayed=True)
    assert claim.lease_id
    lease_id = claim.lease_id
    state = claim.state
    evidence = DraftEvidence(
        draft_id=str(state.get("draft_id") or ""),
        message_id=str(state.get("message_id") or "") or None,
    )

    try:
        if evidence.draft_id:
            provider_state = await inspect_delivery(evidence)
            if isinstance(provider_state, SentEvidence):
                sent_state = await state_store.update_state(
                    context,
                    lease_id,
                    {
                        "status": "sent",
                        "message_id": provider_state.message_id,
                        "thread_id": provider_state.thread_id,
                        "recovered_from_provider": True,
                    },
                    release=True,
                )
                return _safe_result(sent_state, replayed=True)
            if provider_state is None:
                raise GmailDeliveryIndeterminate(
                    "Gmail delivery outcome is indeterminate; refusing to send again"
                )
            # ``False`` means the draft still exists and is safe to send.
        else:
            evidence = await create_draft(envelope, context.idempotency_sha256)
            if not evidence.draft_id:
                raise RuntimeError("Gmail draft creation returned no draft id")
            state = await state_store.update_state(
                context,
                lease_id,
                {
                    "status": "drafted",
                    "draft_id": evidence.draft_id,
                    "message_id": evidence.message_id,
                },
            )

        sent = await send_draft(evidence)
        if not sent.message_id:
            raise GmailDeliveryIndeterminate("Gmail send returned no message id")
        sent_state = await state_store.update_state(
            context,
            lease_id,
            {
                "status": "sent",
                "draft_id": evidence.draft_id,
                "message_id": sent.message_id,
                "thread_id": sent.thread_id,
                "recovered_from_provider": False,
            },
            release=True,
        )
        return _safe_result(sent_state, replayed=False)
    except Exception:
        # Keep provider IDs, but release the execution lease so a retry can
        # inspect provider state. If persistence itself is unavailable, the
        # original exception is still safer than attempting another send.
        try:
            await state_store.update_state(context, lease_id, {}, release=True)
        except Exception:
            pass
        raise
