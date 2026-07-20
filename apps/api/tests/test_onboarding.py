"""W1 Phase 4 — onboarding state endpoints."""
from __future__ import annotations

import uuid
import httpx
import pytest
from sqlalchemy import select

import main
from core.auth import create_access_token
from core.db import engine, reflect_table


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


def _ready_report() -> dict:
    return {
        "status": "ready",
        "can_complete_onboarding": True,
        "blockers": [],
    }


async def _org_and_admin(state: str = "new"):
    org_id = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(
            id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T", onboarding_state=state))
        await conn.execute(members.insert().values(
            id=mid, organization_id=org_id, email=f"{mid[:8]}@t.io", role="admin"))
    return org_id, mid, create_access_token(mid, org_id=org_id), f"o{org_id[:8]}"


@pytest.mark.asyncio
async def test_get_onboarding_state():
    org_id, _, token, sub = await _org_and_admin(state="new")
    async with _client() as client:
        resp = await client.get("/settings/onboarding",
                                headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": sub})
    assert resp.status_code == 200
    assert resp.json()["state"] == "new"


@pytest.mark.asyncio
async def test_first_use_guide_is_server_derived_and_tenant_scoped():
    org_id, _, token, sub = await _org_and_admin(state="complete")
    headers = {"Authorization": f"Bearer {token}", "X-Chronos-Org": sub}
    projects = await reflect_table("projects")
    async with engine.begin() as conn:
        await conn.execute(
            projects.insert().values(
                organization_id=org_id,
                name="First project",
                visibility="private",
                default_tools=[],
            )
        )
    async with _client() as client:
        response = await client.get("/settings/onboarding/guide", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 6
    steps = {step["id"]: step for step in payload["steps"]}
    assert steps["project"]["complete"] is True
    assert steps["project"]["evidence_count"] == 1
    assert steps["connector"]["complete"] is False
    assert steps["schedule"]["href"] == "/workflows?onboarding=schedule"


@pytest.mark.asyncio
async def test_complete_onboarding_sets_state_and_persists(monkeypatch):
    async def ready(**_kwargs):
        return _ready_report()

    monkeypatch.setattr(
        "routers.settings.runtime_health.build_runtime_health_report", ready
    )
    org_id, _, token, sub = await _org_and_admin(state="new")
    headers = {"Authorization": f"Bearer {token}", "X-Chronos-Org": sub}
    async with _client() as client:
        resp = await client.post("/settings/onboarding/complete", headers=headers)
        assert resp.status_code == 200 and resp.json()["state"] == "complete"
        again = await client.get("/settings/onboarding", headers=headers)
    assert again.json()["state"] == "complete"
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        row = (await conn.execute(orgs.select().where(orgs.c.id == org_id))).mappings().one()
    assert row["onboarding_state"] == "complete"


@pytest.mark.asyncio
async def test_complete_onboarding_blocks_when_required_runtime_is_unavailable(
    monkeypatch,
):
    async def blocked(**_kwargs):
        return {
            "status": "blocked",
            "can_complete_onboarding": False,
            "blockers": [
                {"id": "database", "label": "Database", "status": "unavailable"}
            ],
        }

    monkeypatch.setattr(
        "routers.settings.runtime_health.build_runtime_health_report", blocked
    )
    org_id, _, token, sub = await _org_and_admin(state="new")
    headers = {"Authorization": f"Bearer {token}", "X-Chronos-Org": sub}
    async with _client() as client:
        response = await client.post("/settings/onboarding/complete", headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "runtime_not_ready"
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        state = (
            await conn.execute(
                select(organizations.c.onboarding_state).where(
                    organizations.c.id == org_id
                )
            )
        ).scalar_one()
    assert state == "new"


@pytest.mark.asyncio
async def test_complete_onboarding_rejects_non_admin():
    org_id = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T"))
        await conn.execute(members.insert().values(id=mid, organization_id=org_id, email=f"{mid[:8]}@t.io", role="user"))
    token = create_access_token(mid, org_id=org_id)
    async with _client() as client:
        resp = await client.post("/settings/onboarding/complete",
                                 headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": f"o{org_id[:8]}"})
    assert resp.status_code == 403
