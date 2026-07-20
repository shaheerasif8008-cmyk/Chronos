"""Offline proof for approval-bound, exactly-once Gmail delivery."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from connectors import composio_client, gmail, gmail_delivery
from connectors.composio_connector import composio_connector
from connectors.gmail_delivery import (
    ApprovalDeliveryStore,
    DeliveryClaim,
    DeliveryContext,
    DraftEvidence,
    EmailEnvelope,
    SentEvidence,
    deliver_approved_email,
    validate_email_args,
)
from core.exceptions import ApprovalRequired, SafetyLimitViolation
from core.db import engine, reflect_table
from core.models import AgentContext, ToolResult


class MemoryApprovalStore:
    """Small protocol-compatible store; provider behavior remains fully mocked."""

    def __init__(self) -> None:
        self.states: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.claims = 0

    @staticmethod
    def key(context: DeliveryContext) -> tuple[str, str, str]:
        return context.organization_id, context.member_id, context.approval_id

    async def claim(self, context: DeliveryContext, envelope: EmailEnvelope) -> DeliveryClaim:
        self.claims += 1
        key = self.key(context)
        state = self.states.get(key)
        expected = {
            "organization_id": context.organization_id,
            "member_id": context.member_id,
            "credential_scope_sha256": context.credential_scope_sha256,
            "idempotency_sha256": context.idempotency_sha256,
            "payload_sha256": envelope.payload_sha256,
        }
        if state:
            if any(state.get(k) != v for k, v in expected.items()):
                raise SafetyLimitViolation("scope mismatch")
            if state.get("status") == "sent":
                return DeliveryClaim(lease_id=None, state=dict(state), replayed=True)
        else:
            state = dict(expected)
        state["status"] = state.get("status") if state.get("draft_id") else "claimed"
        state["lease_id"] = f"lease-{self.claims}"
        self.states[key] = state
        return DeliveryClaim(lease_id=state["lease_id"], state=dict(state))

    async def update_state(
        self,
        context: DeliveryContext,
        lease_id: str,
        changes: dict[str, Any],
        *,
        release: bool = False,
    ) -> dict[str, Any]:
        state = self.states[self.key(context)]
        assert state["lease_id"] == lease_id
        state.update(changes)
        if release:
            state.pop("lease_id", None)
        return dict(state)


def _context(*, member: str = "member-1", approval: str = "approval-1") -> DeliveryContext:
    return DeliveryContext(
        approval_id=approval,
        organization_id="org-1",
        member_id=member,
        task_id="task-1",
        credential_scope="direct:vault-1",
        idempotency_key="idem-1",
    )


def _envelope() -> EmailEnvelope:
    return validate_email_args(
        {"to": "Client@Example.com", "subject": "Launch", "body": "Ready."}
    )


@pytest.mark.asyncio
async def test_durable_store_binds_approval_to_tenant_task_member_and_payload():
    suffix = uuid.uuid4().hex
    org_id = f"gmail-org-{suffix}"
    member_id = f"gmail-member-{suffix}"
    task_id = f"gmail-task-{suffix}"
    approval_id = f"gmail-approval-{suffix}"
    envelope = _envelope()
    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    tasks = await reflect_table("tasks")
    approvals = await reflect_table("approvals")
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        await conn.execute(
            organizations.insert().values(
                id=org_id,
                organization_id=org_id,
                slug=f"gmail-{suffix}",
                name="Gmail delivery test",
            )
        )
        await conn.execute(
            members.insert().values(
                id=member_id,
                organization_id=org_id,
                email=f"{suffix}@example.com",
                role="admin",
            )
        )
        await conn.execute(
            tasks.insert().values(
                id=task_id,
                organization_id=org_id,
                region="us",
                triggered_by="manual",
                triggered_by_member_id=member_id,
                status="awaiting_approval",
                goal="send email",
            )
        )
        await conn.execute(
            approvals.insert().values(
                id=approval_id,
                organization_id=org_id,
                region="us",
                task_id=task_id,
                step_id="agent_loop",
                action_type="gmail.send",
                action_payload={
                    "tool": "gmail.send",
                    "args": {
                        "to": "client@example.com",
                        "subject": "Launch",
                        "body": "Ready.",
                    },
                },
                status="approved",
                decided_by=member_id,
                decided_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )

    context = DeliveryContext(
        approval_id=approval_id,
        organization_id=org_id,
        member_id=member_id,
        task_id=task_id,
        credential_scope="direct:vault-1",
        idempotency_key="idem-1",
    )
    store = ApprovalDeliveryStore()
    claim = await store.claim(context, envelope)
    assert claim.lease_id
    assert claim.state["organization_id"] == org_id
    assert claim.state["member_id"] == member_id
    assert "client@example.com" not in str(claim.state)

    with pytest.raises(ApprovalRequired, match="credential-owning member"):
        await store.claim(
            DeliveryContext(
                approval_id=approval_id,
                organization_id=org_id,
                member_id="another-member",
                task_id=task_id,
                credential_scope="direct:vault-1",
                idempotency_key="idem-1",
            ),
            envelope,
        )

    from runtime.agent_loop import _mark_approval

    stale_payload = {
        "tool": "gmail.send",
        "args": {
            "to": "client@example.com",
            "subject": "Launch",
            "body": "Ready.",
        },
        "execution_result": {"data": {"message_id": "message-1"}},
    }
    await _mark_approval(approval_id, stale_payload, approvals)
    async with engine.begin() as conn:
        persisted = (
            await conn.execute(
                approvals.select().where(approvals.c.id == approval_id)
            )
        ).mappings().one()
    assert persisted["action_payload"]["gmail_delivery"]["lease_id"] == claim.lease_id
    assert persisted["action_payload"]["execution_result"]["data"]["message_id"] == "message-1"


@pytest.mark.asyncio
async def test_delivery_creates_draft_then_sends_once_and_replays_ids():
    store = MemoryApprovalStore()
    calls: list[tuple[str, str]] = []

    async def create(envelope: EmailEnvelope, evidence_hash: str) -> DraftEvidence:
        calls.append(("create", evidence_hash))
        assert envelope.to == ("client@example.com",)
        return DraftEvidence("draft-1", "message-draft-1")

    async def inspect(_evidence: DraftEvidence):
        calls.append(("inspect", "unexpected"))
        return False

    async def send(evidence: DraftEvidence) -> SentEvidence:
        calls.append(("send", evidence.draft_id))
        return SentEvidence("message-sent-1", "thread-1")

    first = await deliver_approved_email(
        context=_context(),
        envelope=_envelope(),
        create_draft=create,
        inspect_delivery=inspect,
        send_draft=send,
        store=store,
    )
    replay = await deliver_approved_email(
        context=_context(),
        envelope=_envelope(),
        create_draft=create,
        inspect_delivery=inspect,
        send_draft=send,
        store=store,
    )

    assert [call[0] for call in calls] == ["create", "send"]
    assert first.data == {
        "status": "sent",
        "message_id": "message-sent-1",
        "draft_id": "draft-1",
        "thread_id": "thread-1",
        "idempotency_evidence": _context().idempotency_sha256,
        "replayed": False,
        "recovered_from_provider": False,
    }
    assert replay.data["replayed"] is True
    assert replay.data["message_id"] == "message-sent-1"
    assert "client@example.com" not in str(first.data)
    assert "Ready" not in str(first.data)


@pytest.mark.asyncio
async def test_delivery_recovers_sent_message_after_crash_without_sending_again():
    store = MemoryApprovalStore()
    context = _context()
    envelope = _envelope()
    store.states[store.key(context)] = {
        "organization_id": context.organization_id,
        "member_id": context.member_id,
        "credential_scope_sha256": context.credential_scope_sha256,
        "idempotency_sha256": context.idempotency_sha256,
        "payload_sha256": envelope.payload_sha256,
        "status": "drafted",
        "draft_id": "draft-1",
        "message_id": "message-draft-1",
    }
    sends = 0

    async def no_create(_envelope: EmailEnvelope, _hash: str) -> DraftEvidence:
        raise AssertionError("must not create another draft")

    async def inspect(evidence: DraftEvidence):
        assert evidence == DraftEvidence("draft-1", "message-draft-1")
        return SentEvidence("message-draft-1", "thread-1")

    async def no_send(_evidence: DraftEvidence) -> SentEvidence:
        nonlocal sends
        sends += 1
        raise AssertionError("must not send twice")

    result = await deliver_approved_email(
        context=context,
        envelope=envelope,
        create_draft=no_create,
        inspect_delivery=inspect,
        send_draft=no_send,
        store=store,
    )

    assert sends == 0
    assert result.data["replayed"] is True
    assert result.data["recovered_from_provider"] is True


def test_recipient_subject_and_body_limits_are_hard_failures():
    with pytest.raises(SafetyLimitViolation, match="11 recipients"):
        validate_email_args(
            {
                "to": [f"user{i}@example.com" for i in range(11)],
                "subject": "Hi",
                "body": "Hello",
            }
        )
    with pytest.raises(SafetyLimitViolation, match="body exceeds"):
        validate_email_args(
            {"to": "a@example.com", "subject": "Hi", "body": "é" * 50_001}
        )
    with pytest.raises(SafetyLimitViolation, match="header newline"):
        validate_email_args(
            {"to": "a@example.com", "subject": "Hi\nBcc: x@example.com", "body": "Hello"}
        )


@pytest.mark.asyncio
async def test_direct_connector_still_rejects_unapproved_send():
    with pytest.raises(ApprovalRequired):
        await gmail.gmail_connector.execute(
            "gmail.send",
            {
                "to": "client@example.com",
                "subject": "Hi",
                "body": "Hello",
                "__connector_tier": "live",
            },
            "vault-1",
        )


@pytest.mark.asyncio
async def test_direct_provider_path_uses_drafts_send_and_audit_safe_result(monkeypatch):
    store = MemoryApprovalStore()
    monkeypatch.setattr(gmail_delivery, "ApprovalDeliveryStore", lambda: store)
    from core.config import settings

    monkeypatch.setattr(settings, "demo_mode", False)
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def fake_call(vault_ref, org_id, method, path, *, params=None, json_body=None):
        assert (vault_ref, org_id) == ("vault-1", "org-1")
        calls.append((method, path, json_body))
        if path == "/drafts":
            return {"id": "draft-1", "message": {"id": "message-draft-1"}}
        if path == "/drafts/send":
            return {"id": "message-sent-1", "threadId": "thread-1"}
        raise AssertionError(path)

    monkeypatch.setattr(gmail, "_gmail_call_with_refresh", fake_call)
    result = await gmail.gmail_connector.execute(
        "gmail.send",
        {
            "to": "client@example.com",
            "subject": "Hi",
            "body": "Sensitive body",
            "__approved_by_gate": True,
            "__approval_id": "approval-1",
            "__idempotency_key": "idem-1",
            "__connector_tier": "live",
            "__org_id": "org-1",
            "__task_id": "task-1",
            "__member_id": "member-1",
        },
        "vault-1",
    )

    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/drafts"),
        ("POST", "/drafts/send"),
    ]
    assert calls[1][2] == {"id": "draft-1"}
    assert result.data["message_id"] == "message-sent-1"
    assert "Sensitive body" not in str(result.data)


@pytest.mark.asyncio
async def test_direct_provider_recovers_changed_message_id_by_rfc822_evidence(monkeypatch):
    store = MemoryApprovalStore()
    context = _context()
    envelope = _envelope()
    store.states[store.key(context)] = {
        "organization_id": context.organization_id,
        "member_id": context.member_id,
        "credential_scope_sha256": context.credential_scope_sha256,
        "idempotency_sha256": context.idempotency_sha256,
        "payload_sha256": envelope.payload_sha256,
        "status": "drafted",
        "draft_id": "draft-1",
        "message_id": "old-draft-message",
    }
    monkeypatch.setattr(gmail_delivery, "ApprovalDeliveryStore", lambda: store)
    from core.config import settings

    monkeypatch.setattr(settings, "demo_mode", False)
    calls: list[tuple[str, str]] = []

    async def fake_call(_vault_ref, _org_id, method, path, *, params=None, json_body=None):
        calls.append((method, path))
        if path in {"/drafts/draft-1", "/messages/old-draft-message"}:
            raise gmail._GmailNotFound()
        if path == "/messages" and params and str(params.get("q", "")).startswith("rfc822msgid:"):
            return {"messages": [{"id": "new-sent-message"}]}
        if path == "/messages/new-sent-message":
            return {
                "id": "new-sent-message",
                "threadId": "thread-1",
                "labelIds": ["SENT"],
            }
        raise AssertionError((method, path, json_body))

    monkeypatch.setattr(gmail, "_gmail_call_with_refresh", fake_call)
    result = await gmail.gmail_connector.execute(
        "gmail.send",
        {
            "to": "Client@Example.com",
            "subject": "Launch",
            "body": "Ready.",
            "__approved_by_gate": True,
            "__approval_id": "approval-1",
            "__idempotency_key": "idem-1",
            "__connector_tier": "live",
            "__org_id": "org-1",
            "__task_id": "task-1",
            "__member_id": "member-1",
        },
        "vault-1",
    )

    assert all(method == "GET" for method, _path in calls)
    assert result.data["message_id"] == "new-sent-message"
    assert result.data["recovered_from_provider"] is True


@pytest.mark.asyncio
async def test_composio_provider_path_is_draft_first_and_member_scoped(monkeypatch):
    store = MemoryApprovalStore()
    monkeypatch.setattr(gmail_delivery, "ApprovalDeliveryStore", lambda: store)
    monkeypatch.setattr(composio_client, "is_configured", lambda: True)
    monkeypatch.setattr(composio_client.settings, "composio_entity_scope", "member")
    calls: list[tuple[str, dict[str, Any], str]] = []

    async def fake_execute(action, params, *, entity):
        calls.append((action, params, entity))
        if action == "GMAIL_CREATE_EMAIL_DRAFT":
            return {
                "successful": True,
                "data": {"draft_id": "draft-c1", "message": {"id": "message-c1"}},
            }
        if action == "GMAIL_SEND_DRAFT":
            return {
                "successful": True,
                "data": {"message_id": "message-c1", "thread_id": "thread-c1"},
            }
        raise AssertionError(action)

    monkeypatch.setattr(composio_client, "execute_action", fake_execute)
    agent = AgentContext(
        id="task:task-1",
        org_id="org-1",
        member_id="member-1",
        task_id="task-1",
    )
    result = await composio_connector.execute(
        "gmail.send",
        {
            "to": ["a@example.com", "b@example.com"],
            "cc": "c@example.com",
            "subject": "Hi",
            "body": "Hello",
            "__approved_by_gate": True,
            "__approval_id": "approval-1",
            "__idempotency_key": "idem-1",
            "__connector_tier": "live",
            "__org_id": "org-1",
            "__task_id": "task-1",
            "__member_id": "member-1",
        },
        agent,
    )

    assert [call[0] for call in calls] == [
        "GMAIL_CREATE_EMAIL_DRAFT",
        "GMAIL_SEND_DRAFT",
    ]
    assert all(call[2] == "org-1:member-1" for call in calls)
    assert calls[0][1]["recipient_email"] == "a@example.com"
    assert calls[0][1]["extra_recipients"] == ["b@example.com"]
    assert calls[0][1]["cc"] == ["c@example.com"]
    assert result.data["message_id"] == "message-c1"


def test_composio_send_resolver_cannot_bypass_draft_identifier():
    action, params = composio_client.resolve_action("gmail.send", {"draft_id": "draft-1"})
    assert action == "GMAIL_SEND_DRAFT"
    assert params == {"draft_id": "draft-1"}


@pytest.mark.asyncio
async def test_broker_forwards_only_internal_approval_context_to_send(monkeypatch):
    from core import tool_broker

    captured: dict[str, Any] = {}

    async def noop(*_args, **_kwargs):
        return None

    async def allow(*_args, **_kwargs):
        return True

    async def enabled(*_args, **_kwargs):
        return {"enabled": True}

    async def empty_permissions(*_args, **_kwargs):
        return {}

    async def supervised(*_args, **_kwargs):
        return "supervised"

    async def fixture(_provider):
        return "fixture"

    async def no_degraded(_provider):
        return None

    async def route(agent, tool, args, vault_ref, tier="live"):
        captured.update(agent=agent, tool=tool, args=dict(args), vault_ref=vault_ref, tier=tier)
        return ToolResult(data={"message_id": "message-1"}, summary="sent")

    async def no_cache(*_args, **_kwargs):
        return None

    async def no_overrides(*_args, **_kwargs):
        return {}

    async def no_history(*_args, **_kwargs):
        return SimpleNamespace(successes=0)

    monkeypatch.setattr(tool_broker.permissions, "check", allow)
    monkeypatch.setattr(tool_broker, "_check_rate_limit", noop)
    monkeypatch.setattr(tool_broker, "_check_loop", noop)
    monkeypatch.setattr(tool_broker, "tool_policy", enabled)
    monkeypatch.setattr(tool_broker, "workspace_autonomy", supervised)
    monkeypatch.setattr(tool_broker, "connector_tier", fixture)
    monkeypatch.setattr(tool_broker, "degraded_note", no_degraded)
    monkeypatch.setattr(tool_broker, "_route", route)
    monkeypatch.setattr(tool_broker, "_load_idempotent_result", no_cache)
    monkeypatch.setattr(tool_broker, "_store_idempotent_result", noop)
    monkeypatch.setattr(tool_broker.audit, "log", noop)
    monkeypatch.setattr(tool_broker.risk_registry, "get_overrides", no_overrides)
    monkeypatch.setattr(tool_broker.trust, "get_trust_level", no_history)
    monkeypatch.setattr(tool_broker.trust, "novelty_from_successes", lambda _n: 1.0)
    monkeypatch.setattr(tool_broker.trust, "record_outcome", noop)
    monkeypatch.setattr(
        "core.settings_store.tool_permissions",
        empty_permissions,
    )

    agent = AgentContext(
        id="task:task-1",
        org_id="org-1",
        member_id="member-1",
        task_id="task-1",
    )
    result = await tool_broker.execute(
        agent,
        "gmail.send",
        {
            "to": "client@example.com",
            "subject": "Hi",
            "body": "Hello",
            "__approved_by_gate": True,
            "__approval_id": "approval-1",
            "__idempotency_key": "idem-1",
        },
    )

    assert result.summary == "sent"
    assert captured["args"] == {
        "to": "client@example.com",
        "subject": "Hi",
        "body": "Hello",
        "__approved_by_gate": True,
        "__approval_id": "approval-1",
        "__idempotency_key": "idem-1",
    }
    assert tool_broker._cache_key(
        "org-1", "gmail.send", "idem-1", member_id="member-1"
    ) != (
        tool_broker._cache_key(
            "org-1", "gmail.send", "idem-1", member_id="member-2"
        )
    )

    with pytest.raises(SafetyLimitViolation, match="approval and idempotency evidence"):
        await tool_broker.execute(
            agent,
            "gmail.send",
            {
                "to": "client@example.com",
                "subject": "Hi",
                "body": "Hello",
                "__approved_by_gate": True,
                "__approval_id": "approval-1",
            },
        )
