from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import httpx
from sqlalchemy import delete, insert, select
from fastapi import HTTPException
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from core import notification_delivery
from core.db import engine, reflect_table
from core.models import Member
from routers import agents as agents_router
from routers.approvals import ApprovalDecision, decide_approval
import main


def test_provider_payload_rejects_content_credentials_and_addresses() -> None:
    notification_delivery._assert_metadata_only(
        {"thread_id": "1712345.001", "external_message_id_hash": "a" * 64}
    )
    for unsafe in (
        {"message": "customer content"},
        {"credential": "bearer abc"},
        {"destination_id": "person@example.com"},
        {"nested": {"webhook_token": "secret"}},
    ):
        with pytest.raises(ValueError):
            notification_delivery._assert_metadata_only(unsafe)


@pytest.mark.asyncio
async def test_stale_chat_provider_claim_dead_letters_without_duplicate_retry() -> None:
    organization_id = f"pub-crash-{uuid.uuid4().hex[:10]}"
    member_id = f"member-{uuid.uuid4().hex[:10]}"
    binding_id = str(uuid.uuid4())
    receipt_id = f"receipt-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    bindings = await reflect_table("agent_publication_bindings")
    receipts = await reflect_table("notification_delivery_receipts")
    async with engine.begin() as conn:
        await conn.execute(
            insert(organizations).values(
                id=organization_id,
                slug=f"slug-{organization_id}",
                name="Provider crash test",
            )
        )
        await conn.execute(
            insert(members).values(
                id=member_id,
                organization_id=organization_id,
                email=f"{member_id}@example.com",
                role="admin",
            )
        )
        await conn.execute(
            insert(bindings).values(
                id=binding_id,
                organization_id=organization_id,
                member_id=member_id,
                provider="slack",
                connector_id="slack-test",
                external_tenant_id="T-test",
                external_channel_id="C-test",
                status="active",
                provider_status="ready",
                created_by=member_id,
            )
        )
        await conn.execute(
            insert(receipts).values(
                id=receipt_id,
                organization_id=organization_id,
                notification_id=None,
                member_id=member_id,
                delivery_kind="notification",
                channel="slack",
                dedupe_key=f"crash:{receipt_id}",
                recipient="C-test",
                subject="Status",
                body="A durable notification",
                status="processing",
                attempts=1,
                max_attempts=5,
                claimed_at=now - timedelta(minutes=11),
                claim_token="abandoned-worker",
                binding_id=binding_id,
                provider_payload={"notification_type": "security"},
            )
        )
    try:
        claimed = await notification_delivery._claim_receipts(
            organization_id=organization_id,
            limit=10,
            now=now,
            delivery_kind="notification",
        )
        assert claimed == []
        async with engine.begin() as conn:
            row = (
                await conn.execute(select(receipts).where(receipts.c.id == receipt_id))
            ).mappings().one()
        assert row["status"] == "dead_letter"
        assert row["last_error_code"] == "ambiguous_provider_outcome"
        assert row["attempts"] == 1
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(receipts).where(receipts.c.organization_id == organization_id))
            await conn.execute(delete(bindings).where(bindings.c.organization_id == organization_id))
            await conn.execute(delete(members).where(members.c.organization_id == organization_id))
            await conn.execute(delete(organizations).where(organizations.c.id == organization_id))


@pytest.mark.asyncio
async def test_external_reply_policy_gates_web_response_until_approval() -> None:
    organization_id = f"pub-approval-{uuid.uuid4().hex[:10]}"
    member_id = f"member-{uuid.uuid4().hex[:10]}"
    agent_id = str(uuid.uuid4())
    publication_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    conversation_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    profiles = await reflect_table("agent_profiles")
    publications = await reflect_table("agent_publications")
    workspaces = await reflect_table("workspaces")
    tasks = await reflect_table("tasks")
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    receipts = await reflect_table("notification_delivery_receipts")
    approvals = await reflect_table("approvals")
    async with engine.begin() as conn:
        await conn.execute(insert(organizations).values(id=organization_id, slug=f"slug-{organization_id}", name="Approval test"))
        await conn.execute(insert(members).values(id=member_id, organization_id=organization_id, email=f"{member_id}@example.com", role="admin"))
        await conn.execute(
            insert(workspaces).values(
                id=workspace_id,
                organization_id=organization_id,
                name="Default",
                slug="default",
                legacy_key="default",
                status="active",
                created_by=member_id,
            )
        )
        await conn.execute(insert(profiles).values(id=agent_id, organization_id=organization_id, name="Support", role="support", instructions="Help", created_by=member_id))
        await conn.execute(
            insert(publications).values(
                id=publication_id,
                organization_id=organization_id,
                agent_profile_id=agent_id,
                target="web",
                display_name="Support",
                config={},
                approval_policy={"external_replies": "require_approval"},
                allowed_origins=["https://example.com"],
                status="active",
                provider_status="ready",
                created_by=member_id,
            )
        )
        await conn.execute(
            insert(conversations).values(
                id=conversation_id,
                organization_id=organization_id,
                member_id=member_id,
                title="Published support thread",
                workspace_id=workspace_id,
            )
        )
        await conn.execute(
            insert(messages).values(
                organization_id=organization_id,
                conversation_id=conversation_id,
                role="assistant",
                content="Exact customer response",
            )
        )
        await conn.execute(
            insert(tasks).values(
                id=task_id,
                organization_id=organization_id,
                triggered_by=conversation_id,
                triggered_by_member_id=member_id,
                status="complete",
                goal="Reply",
                plan={},
                result={},
                agent_state={"agent_publication": {"id": publication_id, "conversation_id": conversation_id}},
                current_step=0,
                depth=0,
            )
        )

    assert await notification_delivery.enqueue_agent_response(task_id, "Exact customer response") is True
    async with engine.begin() as conn:
        receipt = (await conn.execute(select(receipts).where(receipts.c.task_id == task_id))).mappings().one()
        approval = (await conn.execute(select(approvals).where(approvals.c.id == receipt["approval_id"]))).mappings().one()
    assert receipt["status"] == "approval_pending"
    assert receipt["provider_payload"] == {"external_message_id_hash": hashlib.sha256(b"").hexdigest()}
    assert "Exact customer response" not in str(receipt["provider_payload"])
    assert approval["action_payload"]["body"] == "Exact customer response"
    gated = await agents_router._publication_task_result(
        {"id": publication_id, "organization_id": organization_id}, task_id
    )
    assert gated["response_status"] == "approval_pending"
    assert gated["answer"] is None

    decision = await decide_approval(
        str(approval["id"]),
        ApprovalDecision(decision="approved"),
        Member(id=member_id, organization_id=organization_id, email=f"{member_id}@example.com", role="admin"),
    )
    assert decision["publication_response_released"] is True
    assert decision["resuming"] is False
    async with engine.begin() as conn:
        released = (await conn.execute(select(receipts).where(receipts.c.id == receipt["id"]))).mappings().one()
    assert released["status"] == "delivered"
    assert released["delivered_at"] is not None
    visible = await agents_router._publication_task_result(
        {"id": publication_id, "organization_id": organization_id}, task_id
    )
    assert visible["answer"] == "Exact customer response"


@pytest.mark.asyncio
async def test_ambiguous_provider_error_dead_letters_without_retry(monkeypatch) -> None:
    organization_id = f"pub-ambiguous-{uuid.uuid4().hex[:10]}"
    member_id = f"member-{uuid.uuid4().hex[:10]}"
    receipt_id = f"receipt-{uuid.uuid4().hex[:10]}"
    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    receipts = await reflect_table("notification_delivery_receipts")
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        await conn.execute(insert(organizations).values(id=organization_id, slug=f"slug-{organization_id}", name="Ambiguity test"))
        await conn.execute(insert(members).values(id=member_id, organization_id=organization_id, email=f"{member_id}@example.com", role="admin"))
        row = (
            await conn.execute(
                insert(receipts).values(
                    id=receipt_id,
                    organization_id=organization_id,
                    member_id=member_id,
                    delivery_kind="agent_response",
                    channel="slack",
                    dedupe_key=f"ambiguous:{receipt_id}",
                    recipient="C-test",
                    subject="Reply",
                    body="Customer-visible response",
                    status="processing",
                    attempts=1,
                    max_attempts=5,
                    claimed_at=now,
                    claim_token="claim-test",
                    provider_payload={},
                ).returning(receipts)
            )
        ).mappings().one()
    monkeypatch.setattr(
        notification_delivery,
        "_provider_send_channel",
        AsyncMock(side_effect=notification_delivery.AmbiguousProviderDelivery("ambiguous_provider_outcome")),
    )
    outcome = await notification_delivery._dispatch_claimed([dict(row)], now=now)
    assert outcome == {"delivered": 0, "retried": 0, "dead_letter": 1}
    async with engine.begin() as conn:
        stored = (await conn.execute(select(receipts).where(receipts.c.id == receipt_id))).mappings().one()
    assert stored["status"] == "dead_letter"
    assert stored["last_error_code"] == "ambiguous_provider_outcome"
    assert stored["next_attempt_at"] is None


def test_api_publication_scope_floor() -> None:
    read_key = Member(id="m", email="m@example.com", role="admin", auth_type="api_key", api_key_scopes=["read"])
    agents_router._require_api_key_scope(read_key, write=False)
    with pytest.raises(HTTPException) as denied:
        agents_router._require_api_key_scope(read_key, write=True)
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as session_denied:
        agents_router._require_api_key_scope(Member(id="m", email="m@example.com", role="admin"), write=False)
    assert session_denied.value.status_code == 401


def test_sendgrid_inbound_signature_is_timestamped_and_verified(monkeypatch) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setattr(agents_router.settings, "sendgrid_inbound_public_key", public_pem)
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    raw = b"signed multipart payload"
    signature = base64.b64encode(
        private_key.sign(timestamp.encode() + raw, ec.ECDSA(hashes.SHA256()))
    ).decode()
    agents_router._verify_sendgrid_inbound_signature(raw, timestamp, signature)
    with pytest.raises(HTTPException) as invalid:
        agents_router._verify_sendgrid_inbound_signature(raw + b"tampered", timestamp, signature)
    assert invalid.value.status_code == 401


@pytest.mark.asyncio
async def test_signed_sendgrid_inbound_email_creates_tenant_bound_task(monkeypatch) -> None:
    organization_id = f"pub-email-{uuid.uuid4().hex[:10]}"
    member_id = f"member-{uuid.uuid4().hex[:10]}"
    agent_id = str(uuid.uuid4())
    publication_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    inbound_address = f"agent-{uuid.uuid4().hex[:8]}@inbound.example.com"
    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    profiles = await reflect_table("agent_profiles")
    publications = await reflect_table("agent_publications")
    workspaces = await reflect_table("workspaces")
    workspace_members = await reflect_table("workspace_members")
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        await conn.execute(insert(organizations).values(id=organization_id, slug=f"slug-{organization_id}", name="Email publication test"))
        await conn.execute(insert(members).values(id=member_id, organization_id=organization_id, email=f"{member_id}@example.com", role="admin"))
        await conn.execute(insert(workspaces).values(id=workspace_id, organization_id=organization_id, name="Default", slug="default", legacy_key="default", status="active", created_by=member_id))
        await conn.execute(insert(workspace_members).values(organization_id=organization_id, workspace_id=workspace_id, member_id=member_id, role="owner", added_by=member_id))
        await conn.execute(insert(profiles).values(id=agent_id, organization_id=organization_id, name="Email Support", role="support", instructions="Help safely", approval_policy={"external_replies": "require_approval"}, created_by=member_id))
        await conn.execute(insert(publications).values(id=publication_id, organization_id=organization_id, agent_profile_id=agent_id, target="email", display_name="Email Support", external_channel_id=inbound_address, config={}, approval_policy={"external_replies": "require_approval"}, status="active", provider_status="ready", created_by=member_id))

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = private_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setattr(agents_router.settings, "sendgrid_inbound_public_key", public_pem)
    monkeypatch.setattr(agents_router.task_runner, "enqueue_task", AsyncMock())
    boundary = f"chronos-{uuid.uuid4().hex}"
    fields = {
        "envelope": json.dumps({"to": [inbound_address], "from": "customer@example.net"}),
        "to": inbound_address,
        "from": "Customer <customer@example.net>",
        "subject": "Refund policy",
        "text": "Can you summarize the refund policy?",
        "headers": "Message-ID: <email-event-1@example.net>\r\n",
        "attachments": "0",
    }
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    raw = b"".join(chunks)
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    signature = base64.b64encode(private_key.sign(timestamp.encode() + raw, ec.ECDSA(hashes.SHA256()))).decode()
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/agents/publications/{publication_id}/email/events",
            content=raw,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-Twilio-Email-Event-Webhook-Timestamp": timestamp,
                "X-Twilio-Email-Event-Webhook-Signature": signature,
            },
        )
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]
    async with engine.begin() as conn:
        task = (await conn.execute(select(tasks).where(tasks.c.id == task_id, tasks.c.organization_id == organization_id))).mappings().one()
    state = task["agent_state"]["agent_publication"]
    assert state["reply_to_email"] == "customer@example.net"
    assert state["email_subject"] == "Refund policy"
