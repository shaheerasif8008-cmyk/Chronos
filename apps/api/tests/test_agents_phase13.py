from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

import main
from core.auth import create_access_token
from core.db import engine, reflect_table


async def _make_org_member(role: str = "user") -> tuple[str, str, str]:
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"org-{org_id[:8]}", name="Agent Org"))
        await conn.execute(
            members.insert().values(
                id=member_id,
                organization_id=org_id,
                email=f"{member_id[:8]}@agents.test",
                role=role,
            )
        )
    return org_id, member_id, create_access_token(member_id)


async def _make_project(org_id: str, member_id: str) -> str:
    projects = await reflect_table("projects")
    project_members = await reflect_table("project_members")
    project_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            projects.insert().values(
                id=project_id,
                organization_id=org_id,
                region="us",
                name="Executive Briefing",
                instructions="Use only approved executive sources.",
                visibility="private",
                default_tools=["web.search"],
                memory_policy="project",
                created_by=member_id,
            )
        )
        await conn.execute(
            project_members.insert().values(
                organization_id=org_id,
                region="us",
                project_id=project_id,
                member_id=member_id,
                role="owner",
            )
        )
    return project_id


@pytest.mark.asyncio
async def test_agent_profile_create_attach_project_tool_run_and_tenant_scope(monkeypatch):
    org_a, member_a, token_a = await _make_org_member()
    _, _, token_b = await _make_org_member()
    project_id = await _make_project(org_a, member_a)

    monkeypatch.setattr("routers.agents.task_runner.enqueue_task", AsyncMock())

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        templates = await client.get("/agents/templates", headers={"Authorization": f"Bearer {token_a}"})
        assert templates.status_code == 200, templates.text
        assert {"research", "executive_assistant", "sales_sdr", "support", "engineering", "data_analysis", "operations"} <= {
            item["id"] for item in templates.json()
        }

        created = await client.post(
            "/agents",
            json={
                "name": "Executive Research Agent",
                "role": "research analyst",
                "template_id": "research",
                "instructions": "Prepare concise cited briefs.",
                "model": "deepseek-v4-pro",
                "tool_grants": ["web.search", "research.run"],
                "connector_grants": ["slack", "google_drive"],
                "project_ids": [project_id],
                "memory_scopes": [{"scope": "project", "scope_id": project_id}],
                "autonomy_level": "supervised",
                "approval_policy": {"risky_writes": "require_approval", "external_replies": "require_approval"},
                "schedule_permissions": {"allowed": True, "max_frequency": "daily"},
            },
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert created.status_code == 200, created.text
        agent = created.json()
        agent_id = agent["id"]
        assert agent["project_ids"] == [project_id]
        assert agent["tool_grants"] == ["web.search", "research.run"]
        assert agent["approval_policy"]["risky_writes"] == "require_approval"

        listed = await client.get("/agents", headers={"Authorization": f"Bearer {token_a}"})
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()] == [agent_id]

        run = await client.post(
            f"/agents/{agent_id}/run",
            json={"goal": "Draft a weekly exec brief", "project_id": project_id},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert run.status_code == 200, run.text
        task_id = run.json()["task_id"]

        tasks = await reflect_table("tasks")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(tasks).where(tasks.c.id == task_id, tasks.c.organization_id == org_a)
                )
            ).mappings().first()
        assert row is not None
        assert row["triggered_by"] == f"agent:{agent_id}"
        assert str(row["project_id"]) == project_id
        assert row["agent_state"]["agent_profile"]["id"] == agent_id
        assert row["agent_state"]["agent_profile"]["memory_scopes"] == [{"scope": "project", "scope_id": project_id}]

        cross = await client.get(f"/agents/{agent_id}", headers={"Authorization": f"Bearer {token_b}"})
        assert cross.status_code == 404, cross.text


@pytest.mark.asyncio
async def test_agent_publication_external_fixture_creates_audited_chronos_task(monkeypatch):
    org_id, member_id, token = await _make_org_member()
    project_id = await _make_project(org_id, member_id)
    monkeypatch.setattr("routers.agents.task_runner.enqueue_task", AsyncMock())

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/agents",
            json={
                "name": "Support Agent",
                "role": "support specialist",
                "template_id": "support",
                "instructions": "Answer policy questions and escalate billing changes.",
                "model": "deepseek-v4-pro",
                "tool_grants": ["connector.search"],
                "connector_grants": ["slack", "teams", "email"],
                "project_ids": [project_id],
                "memory_scopes": [{"scope": "project", "scope_id": project_id}],
                "autonomy_level": "approval_required",
                "approval_policy": {"external_replies": "require_approval"},
                "schedule_permissions": {"allowed": False},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert created.status_code == 200, created.text
        agent_id = created.json()["id"]

        published = await client.post(
            f"/agents/{agent_id}/publications",
            json={
                "target": "slack",
                "display_name": "Support Triage",
                "external_channel_id": "C-support",
                "config": {"reply_mode": "threaded", "allowed_team": "support"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert published.status_code == 200, published.text
        publication = published.json()
        assert publication["target"] == "slack"
        assert publication["status"] == "active"
        assert publication["approval_policy"]["external_replies"] == "require_approval"

        for target in ["teams", "email", "web", "api"]:
            target_pub = await client.post(
                f"/agents/{agent_id}/publications",
                json={
                    "target": target,
                    "display_name": f"Support Triage {target}",
                    "external_channel_id": f"{target}-fixture",
                    "config": {"fixture": True},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert target_pub.status_code == 200, target_pub.text
            assert target_pub.json()["target"] == target

        inbound = await client.post(
            f"/agents/publications/{publication['id']}/inbound",
            json={
                "external_conversation_id": "slack-thread-1",
                "external_message_id": "msg-1",
                "sender": {"id": "U123", "name": "Customer"},
                "text": "Can this agent summarize our refund policy?",
            },
            headers={"X-Chronos-Agent-Token": publication["inbound_token"]},
        )
        assert inbound.status_code == 200, inbound.text
        body = inbound.json()
        assert body["task_id"]
        assert body["agent_id"] == agent_id
        assert body["publication_id"] == publication["id"]

        tasks = await reflect_table("tasks")
        events = await reflect_table("agent_profile_events")
        audit_log = await reflect_table("audit_log")
        async with engine.begin() as conn:
            task = (
                await conn.execute(
                    select(tasks).where(tasks.c.id == body["task_id"], tasks.c.organization_id == org_id)
                )
            ).mappings().first()
            event_rows = (
                await conn.execute(
                    select(events).where(events.c.agent_profile_id == agent_id, events.c.organization_id == org_id)
                )
            ).mappings().all()
            audit_rows = (
                await conn.execute(
                    select(audit_log).where(audit_log.c.resource_id == body["task_id"], audit_log.c.organization_id == org_id)
                )
            ).mappings().all()

        assert task is not None
        assert task["triggered_by"] == f"agent_publication:{publication['id']}"
        assert task["agent_state"]["agent_publication"]["target"] == "slack"
        assert task["agent_state"]["agent_profile"]["approval_policy"]["external_replies"] == "require_approval"
        assert "slack-thread-1" in task["agent_state"]["agent_publication"]["external_conversation_id"]
        assert {"agent_published", "external_message_received", "agent_publication_task_created"} <= {
            row["event_type"] for row in event_rows
        }
        assert "agent_publication_task_created" in {row["event_type"] for row in audit_rows}
