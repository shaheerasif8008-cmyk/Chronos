"""
Proofs for the JSON error boundary and honest connector errors.

An unhandled exception must come back as JSON *with CORS headers* — without
them the browser cannot read the response cross-origin and surfaces a bare
"Failed to fetch", hiding the real failure. And a Composio SDK failure during
connect must be an honest 502 with a readable detail, not an unhandled 500.
"""
from __future__ import annotations

import uuid

import httpx
import pytest


@pytest.mark.asyncio
async def test_unhandled_exception_returns_json_with_cors_headers():
    import main

    route_path = "/__test_boom"
    if not any(getattr(r, "path", "") == route_path for r in main.app.routes):
        @main.app.get(route_path)
        async def _boom():  # pragma: no cover - body always raises
            raise RuntimeError("intentional test explosion")

    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(route_path, headers={"Origin": "http://localhost:3000"})

    assert resp.status_code == 500
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert resp.json()["detail"].startswith("Internal server error")


@pytest.mark.asyncio
async def test_composio_sdk_failure_yields_clean_502(monkeypatch):
    import main
    from connectors import composio_client
    from core.auth import create_access_token
    from core.db import engine, reflect_table

    monkeypatch.setattr(composio_client, "is_configured", lambda: True)

    class FakeSDKError(Exception):
        pass

    async def exploding_initiate(provider, *, entity, redirect_url):
        raise FakeSDKError("no auth config found for toolkit 'notion'")

    monkeypatch.setattr(composio_client, "initiate_connection", exploding_initiate)

    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"o-{org_id[:8]}", name="O"))
        await conn.execute(
            members.insert().values(
                id=member_id, organization_id=org_id, email=f"{member_id[:8]}@t.io", role="admin"
            )
        )

    auth = {"Authorization": f"Bearer {create_access_token(member_id)}"}
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/connectors/notion/oauth-start", headers=auth)

    assert resp.status_code == 502
    assert "Composio connect failed" in resp.json()["detail"]
    assert "no auth config found" in resp.json()["detail"]
