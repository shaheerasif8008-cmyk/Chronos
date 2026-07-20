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
from tests.workspace_fixtures import ensure_default_workspace


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
    await ensure_default_workspace(org_id, [member_id])
    return org_id, member_id, create_access_token(member_id)


async def _insert_conversation(org_id: str, member_id: str) -> str:
    """Insert a minimal conversation row and return its UUID string."""
    conversations = await reflect_table("conversations")
    workspace_id = await ensure_default_workspace(org_id, [member_id])
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(conversations).values(
                    organization_id=org_id,
                    region="us",
                    member_id=member_id,
                    title="test-conv",
                    workspace_id=workspace_id,
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


# ---------------------------------------------------------------------------
# Test 4: send_message wires artifact_refs through to _save_message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_message_persists_attachment_refs_on_user_message(monkeypatch):
    """send_message must resolve artifact metadata for each attachment_id and
    write it into the artifact_refs kwarg of the user-message _save_message call.

    This proves the new code in the router (the _user_artifact_refs loop) actually
    executes — not just that _save_message accepts the kwarg.

    Uses monkeypatching to avoid DB, LLM, and streaming overhead.
    """
    from fastapi import Response
    from routers import chat as chat_router
    from core.models import Member

    member = Member(id="m1", organization_id="org-test", email="a@b.c", role="user", name="A")

    # Capture the kwargs passed to _save_message.
    save_calls: list[dict] = []

    async def fake_save_message(conv_id, role, content, *, _member_id=None, _org_id=None, **kwargs):
        save_calls.append({"conv_id": conv_id, "role": role, "kwargs": kwargs})
        # Simulate the conv_row return (for existing-conv branch).
        if _member_id is not None:
            return {"id": conv_id, "project_id": None}
        return None

    async def fake_get_artifact(artifact_id):
        return {
            "organization_id": "org-test",
            "title": "report.pdf",
            "kind": "attachment",
            "mime_type": "application/pdf",
            "size_bytes": 1024,
        }

    async def fake_parse_attachments(ids, conv_id, org_id):
        return []

    async def fake_permissions_check(*a, **k):
        return True

    async def fake_workspace_access(*a, **k):
        return {"id": "workspace-test"}

    async def fake_assemble_context(*a, **k):
        return [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

    async def fake_extract_memory(msg):
        return None

    async def fake_stream_chat_turn(**kwargs):
        yield {"type": "done"}

    monkeypatch.setattr(chat_router, "_save_message", fake_save_message)
    monkeypatch.setattr(chat_router, "_get_artifact", fake_get_artifact)
    monkeypatch.setattr(chat_router, "_parse_attachments", fake_parse_attachments)
    monkeypatch.setattr(chat_router.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(
        chat_router.workspace_access,
        "require_workspace_access",
        fake_workspace_access,
    )
    monkeypatch.setattr(chat_router, "assemble_context", fake_assemble_context)
    monkeypatch.setattr(chat_router, "extract_explicit_memory_content", lambda _: None)
    monkeypatch.setattr(chat_router, "normalize_chat_model", lambda m: "fast")
    monkeypatch.setattr(chat_router, "classify_request", lambda _: _wrap_coro({"mode": "chat", "goal": None, "difficulty": "standard", "reasoning_effort": "medium"}))
    monkeypatch.setattr(chat_router, "stream_chat_turn", fake_stream_chat_turn)

    req = chat_router.ChatRequest(
        message="summarize this",
        conversation_id="conv-existing",
        attachment_ids=["att-1"],
    )
    response = await chat_router.send_message(req, member=member)
    # The response is a StreamingResponse; we only care that _save_message was called.
    user_saves = [c for c in save_calls if c["role"] == "user"]
    assert len(user_saves) >= 1, f"_save_message not called for user role: {save_calls}"
    refs = user_saves[0]["kwargs"].get("artifact_refs")
    assert refs is not None, f"artifact_refs not passed to _save_message: {user_saves[0]}"
    assert len(refs) == 1
    assert refs[0]["id"] == "att-1"
    assert refs[0]["kind"] == "attachment"
    assert refs[0]["mime_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_send_message_filters_cross_org_attachment(monkeypatch):
    """Attachments belonging to a different org must not appear in artifact_refs."""
    from routers import chat as chat_router
    from core.models import Member

    member = Member(id="m2", organization_id="org-mine", email="b@b.c", role="user", name="B")

    save_calls: list[dict] = []

    async def fake_save_message(conv_id, role, content, *, _member_id=None, _org_id=None, **kwargs):
        save_calls.append({"role": role, "kwargs": kwargs})
        if _member_id is not None:
            return {"id": conv_id, "project_id": None}
        return None

    async def fake_get_artifact(artifact_id):
        # Belongs to a DIFFERENT org — should be filtered out.
        return {"organization_id": "org-other", "title": "evil.pdf", "kind": "attachment",
                "mime_type": "application/pdf", "size_bytes": 512}

    async def fake_parse_attachments(ids, conv_id, org_id):
        return []

    async def fake_permissions_check(*a, **k):
        return True

    async def fake_workspace_access(*a, **k):
        return {"id": "workspace-test"}

    async def fake_assemble_context(*a, **k):
        return [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

    async def fake_stream_chat_turn(**kwargs):
        yield {"type": "done"}

    monkeypatch.setattr(chat_router, "_save_message", fake_save_message)
    monkeypatch.setattr(chat_router, "_get_artifact", fake_get_artifact)
    monkeypatch.setattr(chat_router, "_parse_attachments", fake_parse_attachments)
    monkeypatch.setattr(chat_router.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(
        chat_router.workspace_access,
        "require_workspace_access",
        fake_workspace_access,
    )
    monkeypatch.setattr(chat_router, "assemble_context", fake_assemble_context)
    monkeypatch.setattr(chat_router, "extract_explicit_memory_content", lambda _: None)
    monkeypatch.setattr(chat_router, "normalize_chat_model", lambda m: "fast")
    monkeypatch.setattr(chat_router, "classify_request", lambda _: _wrap_coro({"mode": "chat", "goal": None, "difficulty": "standard", "reasoning_effort": "medium"}))
    monkeypatch.setattr(chat_router, "stream_chat_turn", fake_stream_chat_turn)

    req = chat_router.ChatRequest(
        message="sneaky",
        conversation_id="conv-existing",
        attachment_ids=["foreign-att"],
    )
    await chat_router.send_message(req, member=member)

    user_saves = [c for c in save_calls if c["role"] == "user"]
    assert len(user_saves) >= 1
    refs = user_saves[0]["kwargs"].get("artifact_refs")
    # Cross-org artifact must be filtered → empty list → we pass None to _save_message.
    assert not refs, f"cross-org artifact leaked into artifact_refs: {refs}"


# ---------------------------------------------------------------------------
# Test 5: Upload with research_run_id audits the link
# ---------------------------------------------------------------------------

@_requires_db
@pytest.mark.asyncio
async def test_upload_with_research_run_id_audits_link():
    """Upload a file with research_run_id → audit event attachment_linked_research_run
    must exist (research_runs has no artifact FK, so audit is the linkage).
    """
    org_id, member_id, token = await _make_org_and_member()

    # Insert a minimal research run.
    research_runs = await reflect_table("research_runs")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(research_runs).values(
                    organization_id=org_id,
                    region="us",
                    member_id=member_id,
                    question="test research question",
                    status="pending",
                ).returning(research_runs.c.id)
            )
        ).first()
    run_id = str(row[0])

    async with _client() as client:
        resp = await client.post(
            "/attachments",
            files={"file": ("context.txt", io.BytesIO(b"research context"), "text/plain")},
            data={"research_run_id": run_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("research_run_id") == run_id
        attachment_id = body["attachment_id"]

    # Verify audit event exists.
    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(audit_log).where(
                    audit_log.c.event_type == "attachment_linked_research_run",
                    audit_log.c.resource_id == attachment_id,
                    audit_log.c.organization_id == org_id,
                )
            )
        ).mappings().first()
    assert row is not None, "No attachment_linked_research_run audit event found"
    assert row["payload"]["research_run_id"] == run_id


@_requires_db
@pytest.mark.asyncio
async def test_cross_org_upload_to_research_run_returns_404():
    """Member from org B cannot link an upload to org A's research run → 404."""
    org_a_id, member_a_id, _ = await _make_org_and_member()
    _, _b_member, token_b = await _make_org_and_member()

    research_runs = await reflect_table("research_runs")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(research_runs).values(
                    organization_id=org_a_id,
                    region="us",
                    member_id=member_a_id,
                    question="org A private research",
                    status="pending",
                ).returning(research_runs.c.id)
            )
        ).first()
    run_id = str(row[0])

    async with _client() as client:
        resp = await client.post(
            "/attachments",
            files={"file": ("steal.txt", io.BytesIO(b"bad actor"), "text/plain")},
            data={"research_run_id": run_id},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 404, (
            f"expected 404 for cross-org research run link, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Helpers for async coroutine wrapping (used by send_message unit tests)
# ---------------------------------------------------------------------------

async def _wrap_coro(val):
    """Return val as a coroutine result (for monkeypatching async functions)."""
    return val
