"""W2.5 — FGA tuple backfill/reconciliation."""
from __future__ import annotations

import uuid
import pytest

from core import permissions
from core.db import engine, reflect_table


async def _seed_org(n_members=2, n_projects=1):
    org_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    pm = await reflect_table("project_members")
    projects = await reflect_table("projects")
    member_ids = []
    async with engine.begin() as conn:
        await conn.execute(
            orgs.insert().values(
                id=org_id,
                slug=f"o{org_id[:8]}",
                subdomain=f"o{org_id[:8]}",
                name="T",
            )
        )
        for i in range(n_members):
            mid = str(uuid.uuid4())
            member_ids.append(mid)
            await conn.execute(
                members.insert().values(
                    id=mid,
                    organization_id=org_id,
                    email=f"{mid[:8]}@t.io",
                    role="owner" if i == 0 else "user",
                )
            )
        for _ in range(n_projects):
            pid = str(uuid.uuid4())
            await conn.execute(
                projects.insert().values(
                    id=pid,
                    organization_id=org_id,
                    name="P",
                )
            )
            await conn.execute(
                pm.insert().values(
                    organization_id=org_id,
                    project_id=pid,
                    member_id=member_ids[0],
                    role="owner",
                )
            )
    return org_id, member_ids


@pytest.mark.asyncio
async def test_reconcile_is_noop_when_fga_disabled(monkeypatch):
    monkeypatch.setattr("core.permissions.settings_openfga_configured", lambda: False)
    org_id, _ = await _seed_org()
    result = await permissions.reconcile_org_tuples(org_id)
    assert result == {
        "members": 0,
        "projects": 0,
        "workspaces": 0,
        "tasks": 0,
        "conversations": 0,
    }


@pytest.mark.asyncio
async def test_reconcile_writes_tuples_per_db_row(monkeypatch):
    # FGA "configured" but stub the actual writers to count calls (no live server needed).
    monkeypatch.setattr("core.permissions.settings_openfga_configured", lambda: True)
    calls = {"org": [], "project": [], "ws_links": []}

    async def fake_org(member_id, org_id, *, admin=False):
        calls["org"].append((member_id, admin))

    async def fake_proj(member_id, role, project_id, org_id):
        calls["project"].append((member_id, project_id, role))

    async def fake_write(tuples):
        calls["ws_links"].extend(tuples)

    monkeypatch.setattr("core.permissions.grant_org_membership", fake_org)
    monkeypatch.setattr("core.permissions.grant_project_role", fake_proj)
    monkeypatch.setattr("core.authz.write_tuples", fake_write)

    org_id, member_ids = await _seed_org(n_members=2, n_projects=1)
    result = await permissions.reconcile_org_tuples(org_id)
    assert result["members"] == 2
    assert result["projects"] == 1
    # workspaces table does not exist in this schema — count must be 0, not an error
    assert result["workspaces"] == 0
    # tasks count must be present (0 tasks seeded in this test — no task rows)
    assert result["tasks"] == 0
    assert result["conversations"] == 0
    # the owner (first member) was granted admin on the org
    assert any(admin for (_mid, admin) in calls["org"])
    assert len(calls["project"]) == 1
