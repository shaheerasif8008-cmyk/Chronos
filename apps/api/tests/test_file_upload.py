"""File upload acceptance tests — Phase 7 Task 1.

Covers:
1. Attachment chips persist on messages across refresh (artifact_refs on user message).
2. Upload with task_id links the artifact to the task and emits an audit event.
3. Cross-org: member from org B cannot link an upload to org A's conversation or task.
"""
from __future__ import annotations

import io
import os
import socket
import uuid

import httpx
import pytest
from sqlalchemy import insert, select

import main
from core.auth import create_access_token
from core.db import engine, reflect_table


# ---------------------------------------------------------------------------
# DB connectivity guard
# ---------------------------------------------------------------------------

def _db_reachable() -> bool:
    host, _, port_str = (
        os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://chronos:chronos@localhost:5432/chronos",
        )
        .rpartition("@")[-1]
        .partition("/")[0]
        .rpartition(":")
    )
    try:
        with socket.create_connection((host or "localhost", int(port_str or 5432)), timeout=1):
            return True
    except OSError:
        return False


_requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _make_org_and_member() -> tuple[str, str, str]:
    """Create a fresh org + member in the test DB. Returns (org_id, member_id, jwt_token)."""
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(
            orgs.insert().values(id=org_id, slug=f"org-{org_id[:8]}", name="Test Org")
        )
        await conn.execute(
            members.insert().values(
                id=member_id,
                organization_id=org_id,
                email=f"{member_id[:8]}@t.io",
                role="user",
            )
        )
    return org_id, member_id, create_access_token(member_id)


async def _insert_conversation(org_id: str, member_id: str) -> str:
    """Insert a minimal conversation row and return its UUID string."""
    conversations = await reflect_table("conversations")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(conversations).values(
                    organization_id=org_id,
                    region="us",
                    member_id=member_id,
                    title="test-conv",
                ).returning(conversations.c.id)
            )
        ).first()
    return str(row[0])


async def _insert_task(org_id: str, member_id: str) -> str:
    """Insert a minimal task row and return its UUID string."""
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(tasks).values(
                    organization_id=org_id,
                    region="us",
                    triggered_by_member_id=member_id,
                    triggered_by="test",
                    goal="test task",
                    status="pending",
                ).returning(tasks.c.id)
            )
        ).first()
    return str(row[0])


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=main.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Test 1: Attachment chips persist on messages across refresh
# ---------------------------------------------------------------------------

@_requires_db
@pytest.mark.asyncio
async def test_attachment_persists_on_user_message_after_refresh():
    """Upload a file → save a user message with artifact_refs → re-fetch from DB
    and verify the attachment id appears in artifact_refs.

    Proves the chip would survive a page refresh (data comes from DB, not React state).
    """
    org_id, member_id, token = await _make_org_and_member()
    conv_id = await _insert_conversation(org_id, member_id)

    async with _client() as client:
        # Step 1: Upload a file linked to the conversation.
        upload_resp = await client.post(
            "/attachments",
            files={"file": ("doc.txt", io.BytesIO(b"important content"), "text/plain")},
            data={"conversation_id": conv_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert upload_resp.status_code == 200, upload_resp.text
        attachment_id = upload_resp.json()["attachment_id"]

    # Step 2: Build artifact_refs (mirroring what send_message now does) and save
    # a user message directly, bypassing the LLM chain.
    from core.artifacts import get_artifact
    from routers.chat import _save_message

    meta = await get_artifact(attachment_id)
    assert meta is not None
    artifact_refs = [{
        "id": attachment_id,
        "title": meta.get("title") or "attachment",
        "kind": meta.get("kind") or "attachment",
        "mime_type": meta.get("mime_type"),
        "size_bytes": meta.get("size_bytes"),
    }]
    await _save_message(conv_id, "user", "here is my doc", artifact_refs=artifact_refs)

    # Step 3: Re-fetch messages from DB (simulates a page refresh).
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(messages).where(messages.c.conversation_id == conv_id)
            )
        ).mappings().all()

    assert len(rows) == 1, f"expected 1 message row, got {len(rows)}"
    row = dict(rows[0])
    refs = row["artifact_refs"]
    assert isinstance(refs, list), f"expected list for artifact_refs, got {type(refs)}: {refs}"
    assert len(refs) == 1, f"expected 1 ref, got {len(refs)}: {refs}"
    assert refs[0]["id"] == attachment_id, (
        f"attachment_id {attachment_id} missing from artifact_refs: {refs}"
    )
    assert refs[0]["kind"] == "attachment"


# ---------------------------------------------------------------------------
# Test 2: Upload with task_id links artifact to the task + audit event
# ---------------------------------------------------------------------------

@_requires_db
@pytest.mark.asyncio
async def test_upload_with_task_id_links_artifact_and_audits():
    """Upload a file with task_id → verify artifact.task_id is set and an
    attachment_linked_task audit event exists.
    """
    org_id, member_id, token = await _make_org_and_member()
    task_id = await _insert_task(org_id, member_id)

    async with _client() as client:
        resp = await client.post(
            "/attachments",
            files={"file": ("data.txt", io.BytesIO(b"task context"), "text/plain")},
            data={"task_id": task_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("task_id") == task_id, f"task_id missing from response: {body}"
        attachment_id = body["attachment_id"]

    # Verify artifact row has task_id set.
    from core.artifacts import get_artifact
    meta = await get_artifact(attachment_id)
    assert meta is not None
    assert str(meta["task_id"]) == task_id, (
        f"artifact.task_id != task_id: got {meta.get('task_id')!r}, wanted {task_id!r}"
    )

    # Verify audit event was written.
    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(audit_log).where(
                    audit_log.c.event_type == "attachment_linked_task",
                    audit_log.c.resource_id == attachment_id,
                    audit_log.c.organization_id == org_id,
                )
            )
        ).mappings().first()
    assert row is not None, "No attachment_linked_task audit event found"
    assert row["payload"]["task_id"] == task_id


# ---------------------------------------------------------------------------
# Test 3: Cross-org upload-link returns 404
# ---------------------------------------------------------------------------

@_requires_db
@pytest.mark.asyncio
async def test_cross_org_upload_to_conversation_returns_404():
    """Member from org B cannot link an upload to org A's conversation → 404."""
    org_a_id, member_a_id, _ = await _make_org_and_member()
    _, _b_member, token_b = await _make_org_and_member()

    # Create a conversation in org A.
    conv_id = await _insert_conversation(org_a_id, member_a_id)

    async with _client() as client:
        resp = await client.post(
            "/attachments",
            files={"file": ("secret.txt", io.BytesIO(b"stolen data"), "text/plain")},
            data={"conversation_id": conv_id},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 404, (
            f"expected 404 for cross-org conversation link, got {resp.status_code}: {resp.text}"
        )


@_requires_db
@pytest.mark.asyncio
async def test_cross_org_upload_to_task_returns_404():
    """Member from org B cannot link an upload to org A's task → 404."""
    org_a_id, member_a_id, _ = await _make_org_and_member()
    _, _b_member, token_b = await _make_org_and_member()

    task_id = await _insert_task(org_a_id, member_a_id)

    async with _client() as client:
        resp = await client.post(
            "/attachments",
            files={"file": ("steal.txt", io.BytesIO(b"bad actor"), "text/plain")},
            data={"task_id": task_id},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 404, (
            f"expected 404 for cross-org task link, got {resp.status_code}: {resp.text}"
        )
