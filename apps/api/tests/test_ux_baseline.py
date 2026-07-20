"""UX-baseline coverage: human-readable approvals, conversation rename, and the
regenerate/retry default-model fallback.

These pin the contract that the normal UI never needs raw payload JSON or
internal identifiers to present an action, and that core chat controls work
with the empty request bodies the frontend actually sends.
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

import main
from core.auth import create_access_token
from core.db import engine, reflect_table
from routers.approvals import approval_summary
from tests.workspace_fixtures import ensure_default_workspace


def test_approval_summary_email_actions_are_plain_english():
    summary = approval_summary(
        "gmail.draft",
        {"args": {"to": "sarah@example.com", "subject": "Follow-up on proposal"}},
    )
    assert "sarah@example.com" in summary
    assert "Follow-up on proposal" in summary
    assert summary.startswith("Create an email draft")

    send = approval_summary("gmail__send", {"to": "a@b.co", "subject": "Hi"})
    assert send.startswith("Send an email")
    assert "a@b.co" in send


def test_approval_summary_generic_action_never_returns_raw_identifiers():
    summary = approval_summary(
        "crm__update_record",
        {"args": {"call_id": "call_991", "target": "Acme deal"}, "agent_loop": True},
    )
    assert "call_991" not in summary
    assert "crm update record" in summary
    assert "Acme deal" in summary

    # Empty payloads still produce a sentence, not an empty string.
    assert approval_summary("", None)


async def _make_org_member_token(role: str = "user") -> tuple[str, str, str]:
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"o-{org_id[:8]}", name="O"))
        await conn.execute(
            members.insert().values(
                id=member_id, organization_id=org_id, email=f"{member_id[:8]}@t.io", role=role
            )
        )
    await ensure_default_workspace(org_id, [member_id])
    return org_id, member_id, create_access_token(member_id)


async def _seed_conversation(org_id: str, member_id: str, title: str = "First message") -> str:
    conversation_id = str(uuid.uuid4())
    conversations = await reflect_table("conversations")
    workspace_id = await ensure_default_workspace(org_id, [member_id])
    async with engine.begin() as conn:
        await conn.execute(
            conversations.insert().values(
                id=conversation_id,
                organization_id=org_id,
                region="us",
                member_id=member_id,
                title=title,
                workspace_id=workspace_id,
            )
        )
    return conversation_id


@pytest.mark.asyncio
async def test_approvals_api_includes_human_readable_summary():
    org_id, _member_id, token = await _make_org_member_token(role="admin")
    task_id = str(uuid.uuid4())
    approval_id = str(uuid.uuid4())
    tasks = await reflect_table("tasks")
    approvals = await reflect_table("approvals")
    async with engine.begin() as conn:
        await conn.execute(
            tasks.insert().values(
                id=task_id, organization_id=org_id, region="us",
                triggered_by="manual", goal="send email", status="awaiting_approval",
            )
        )
        await conn.execute(
            approvals.insert().values(
                id=approval_id, organization_id=org_id, task_id=task_id,
                step_id="agent_loop", action_type="gmail.draft",
                action_payload={"subject": "Quarterly review", "to": "ops@example.com"},
                status="pending",
            )
        )

    auth = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        inbox = await client.get("/approvals/?status=pending", headers=auth)
        assert inbox.status_code == 200, inbox.text
        row = next(a for a in inbox.json() if a["id"] == approval_id)
        assert "ops@example.com" in row["summary"]
        assert "Quarterly review" in row["summary"]

        got = await client.get(f"/approvals/{approval_id}", headers=auth)
        assert got.status_code == 200
        assert got.json()["summary"] == row["summary"]


@pytest.mark.asyncio
async def test_rename_conversation_persists_and_is_scoped_to_owner():
    org_id, member_id, token = await _make_org_member_token()
    conversation_id = await _seed_conversation(org_id, member_id)

    auth = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.patch(
            f"/chat/conversations/{conversation_id}",
            json={"title": "Q3 planning notes"},
            headers=auth,
        )
        assert res.status_code == 200, res.text
        assert res.json()["title"] == "Q3 planning notes"

        listed = await client.get("/chat/conversations", headers=auth)
        titles = {c["id"]: c["title"] for c in listed.json()}
        assert titles[conversation_id] == "Q3 planning notes"

        # Whitespace-only titles are rejected.
        bad = await client.patch(
            f"/chat/conversations/{conversation_id}", json={"title": "   "}, headers=auth
        )
        assert bad.status_code == 422

        # Another org's member cannot rename it.
        _org2, _member2, token2 = await _make_org_member_token()
        cross = await client.patch(
            f"/chat/conversations/{conversation_id}",
            json={"title": "hijacked"},
            headers={"Authorization": f"Bearer {token2}"},
        )
        assert cross.status_code == 404


@pytest.mark.asyncio
async def test_upload_parses_document_eagerly():
    """Uploads kick a background parse so the UI can show ready/unreadable
    states up front instead of discovering them at send time."""
    _org_id, _member_id, token = await _make_org_member_token()
    auth = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/attachments",
            files={"file": ("notes.txt", b"alpha beta gamma", "text/plain")},
            headers=auth,
        )
        assert res.status_code == 200, res.text
        attachment_id = res.json()["attachment_id"]

        meta: dict = {}
        for _ in range(60):
            await asyncio.sleep(0.05)
            meta = (await client.get(f"/artifacts/{attachment_id}", headers=auth)).json()
            if meta.get("parse_status") != "pending":
                break
        assert meta.get("parse_status") == "parsed"


@pytest.mark.asyncio
async def test_regenerate_with_empty_body_uses_default_model(monkeypatch):
    """The UI sends `{}` for regenerate/retry; the turn must fall back to the
    default model instead of failing on `model is required`."""
    org_id, member_id, token = await _make_org_member_token()
    conversation_id = await _seed_conversation(org_id, member_id)
    messages = await reflect_table("messages")
    user_message_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            messages.insert().values(
                id=user_message_id,
                organization_id=org_id,
                region="us",
                conversation_id=conversation_id,
                role="user",
                content="hello there",
            )
        )

    seen_models: list[str | None] = []

    async def fake_stream_chat_turn(**kwargs):
        seen_models.append(kwargs.get("model"))
        yield {"type": "token", "content": "regenerated"}
        yield {"type": "done"}

    monkeypatch.setattr("routers.chat.stream_chat_turn", fake_stream_chat_turn)

    auth = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/chat/conversations/{conversation_id}/messages/{user_message_id}/retry-from-here",
            json={},
            headers=auth,
        )
        assert res.status_code == 200, res.text
        assert res.json()["content"] == "regenerated"

    from core.llm import default_chat_model_id

    assert seen_models == [default_chat_model_id()]
