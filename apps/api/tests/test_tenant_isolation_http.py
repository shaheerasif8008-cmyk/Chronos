"""HTTP-boundary tenant isolation: a member of org B cannot read org A's artifact.

Unlike tests/test_artifact_workspace.py (which calls data functions directly),
this drives the real ASGI app over HTTP — exercising auth (JWT → member → org),
the router, the permission seam, and DB filtering together.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

import main
from core.auth import create_access_token
from core.db import engine, reflect_table


async def _make_org_and_member() -> tuple[str, str]:
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
    return org_id, create_access_token(member_id)


@pytest.mark.asyncio
async def test_cross_org_artifact_access_returns_404_over_http():
    _, token_a = await _make_org_and_member()
    _, token_b = await _make_org_and_member()

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Org A creates an artifact.
        create = await client.post(
            "/artifacts",
            json={"content": "org-a-secret", "kind": "markdown", "title": "secret"},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert create.status_code == 200, create.text
        artifact_id = create.json()["id"]

        # Org A can read its own artifact.
        own = await client.get(
            f"/artifacts/{artifact_id}", headers={"Authorization": f"Bearer {token_a}"}
        )
        assert own.status_code == 200, own.text

        # Org B must NOT see it — cross-tenant access returns 404.
        cross = await client.get(
            f"/artifacts/{artifact_id}", headers={"Authorization": f"Bearer {token_b}"}
        )
        assert cross.status_code == 404, f"cross-org leak: {cross.status_code} {cross.text}"

        # And org B's listing must not include org A's artifact.
        listing = await client.get(
            "/artifacts", headers={"Authorization": f"Bearer {token_b}"}
        )
        assert listing.status_code == 200, listing.text
        assert all(a["id"] != artifact_id for a in listing.json())
