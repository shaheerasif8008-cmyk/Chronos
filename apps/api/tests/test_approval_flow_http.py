"""Deterministic approval-flow coverage (no model involved).

Two halves of the real machinery:
  1. The broker GATE: gmail.draft routes through the real ToolBroker whose gmail
     provider policy is approval_required=True, so it raises ApprovalRequired
     before any connector call.
  2. The DECIDE → RESUME path over HTTP: a seeded pending approval is listed in
     the inbox, decided via the real /approvals/{id}/decide endpoint (real auth),
     flips to "approved", and triggers a task resume (enqueue_task).
"""
from __future__ import annotations

import uuid

import httpx
import pytest

import main
from core import tool_broker
from core.auth import create_access_token
from core.db import engine, reflect_table
from core.exceptions import ApprovalRequired
from core.models import AgentContext


@pytest.mark.asyncio
async def test_gmail_draft_is_gated_by_broker_policy():
    agent = AgentContext(id="task:test", org_id="default", member_id="m1")
    with pytest.raises(ApprovalRequired) as exc:
        await tool_broker.execute(
            agent, "gmail.draft", {"to": "a@example.com", "subject": "Hi", "body": "Hello"}
        )
    assert exc.value.tool == "gmail.draft"


async def _make_org_member_token() -> str:
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"o-{org_id[:8]}", name="O"))
        await conn.execute(
            members.insert().values(
                id=member_id, organization_id=org_id, email=f"{member_id[:8]}@t.io", role="user"
            )
        )
    return org_id, member_id, create_access_token(member_id)


async def _seed_awaiting_task_with_approval(org_id: str) -> tuple[str, str]:
    task_id = str(uuid.uuid4())
    approval_id = str(uuid.uuid4())
    tasks = await reflect_table("tasks")
    approvals = await reflect_table("approvals")
    async with engine.begin() as conn:
        await conn.execute(
            tasks.insert().values(
                id=task_id,
                organization_id=org_id,
                region="us",
                triggered_by="manual",
                goal="send email",
                status="awaiting_approval",
            )
        )
        await conn.execute(
            approvals.insert().values(
                id=approval_id,
                organization_id=org_id,
                task_id=task_id,
                step_id="agent_loop",
                action_type="gmail.draft",
                action_payload={"subject": "E2E approval", "to": "a@example.com"},
                status="pending",
            )
        )
    return task_id, approval_id


@pytest.mark.asyncio
async def test_inbox_decide_flips_to_approved_and_triggers_resume(monkeypatch):
    org_id, _member_id, token = await _make_org_member_token()
    task_id, approval_id = await _seed_awaiting_task_with_approval(org_id)

    # Spy on the resume trigger; no-op the activity emit (keep the test hermetic).
    resumed: list[str] = []

    async def fake_enqueue(tid: str):
        resumed.append(tid)

    async def fake_emit(*args, **kwargs):
        return None

    monkeypatch.setattr("routers.approvals.task_runner.enqueue_task", fake_enqueue)
    monkeypatch.setattr("routers.approvals.emit_activity", fake_emit)

    auth = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Inbox lists the pending approval.
        inbox = await client.get("/approvals/?status=pending", headers=auth)
        assert inbox.status_code == 200, inbox.text
        assert any(a["id"] == approval_id for a in inbox.json())

        # Decide → approved, and the endpoint reports it is resuming.
        decide = await client.post(
            f"/approvals/{approval_id}/decide", json={"decision": "approved"}, headers=auth
        )
        assert decide.status_code == 200, decide.text
        assert decide.json()["resuming"] is True

        # Approval is persisted as approved.
        got = await client.get(f"/approvals/{approval_id}", headers=auth)
        assert got.json()["status"] == "approved"

    # The task resume was triggered exactly once.
    assert resumed == [task_id]
