"""Item 6 — member invitations are real, governed, tenant-scoped, and auditable.

Drives the ASGI app over HTTP for the admin endpoints and exercises the
accept/expiry logic directly against the DB.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import main
from core import invitations
from core.auth import create_access_token
from core.db import engine, reflect_table
from core.models import Member


async def _make_member(role: str = "owner") -> tuple[str, str, str]:
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"org-{org_id[:8]}", name="T"))
        await conn.execute(
            members.insert().values(
                id=member_id, organization_id=org_id, email=f"{member_id[:8]}@t.io", role=role
            )
        )
    return org_id, member_id, create_access_token(member_id)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


@pytest.mark.asyncio
async def test_admin_can_invite_list_and_token_is_never_leaked():
    org_id, _, token = await _make_member("owner")
    invited = f"invitee-{uuid.uuid4().hex[:8]}@t.io"
    async with _client() as client:
        created = await client.post(
            "/settings/invitations",
            json={"email": invited, "role": "viewer"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["status"] == "pending"
        assert body["email"] == invited
        assert body["token"]  # returned once, for delivery

        listed = await client.get("/settings/invitations", headers={"Authorization": f"Bearer {token}"})
        assert listed.status_code == 200
        rows = listed.json()["invitations"]
        assert any(r["email"] == invited for r in rows)
        # The raw token must never come back from the listing endpoint.
        assert all("token" not in r for r in rows)


@pytest.mark.asyncio
async def test_non_admin_cannot_invite():
    _, _, token = await _make_member("user")
    async with _client() as client:
        resp = await client.post(
            "/settings/invitations",
            json={"email": "x@t.io", "role": "viewer"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_invite_validation_and_duplicate_member():
    org_id, member_id, token = await _make_member("owner")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        existing_email = (
            await conn.execute(members.select().where(members.c.id == member_id))
        ).mappings().one()["email"]
    async with _client() as client:
        h = {"Authorization": f"Bearer {token}"}
        assert (await client.post("/settings/invitations", json={"email": "nope", "role": "viewer"}, headers=h)).status_code == 400
        assert (await client.post("/settings/invitations", json={"email": "a@t.io", "role": "wizard"}, headers=h)).status_code == 400
        dup = await client.post("/settings/invitations", json={"email": existing_email, "role": "viewer"}, headers=h)
        assert dup.status_code == 409


@pytest.mark.asyncio
async def test_accept_provisions_member_with_invited_role_once():
    org_id, member_id, _ = await _make_member("owner")
    inviter = Member(id=member_id, organization_id=org_id, email="a@t.io", role="owner")
    email = f"acc-{uuid.uuid4().hex[:8]}@t.io"

    await invitations.create_invitation(inviter, email, "admin")

    member = await invitations.accept_pending_invitation(email, org_id=org_id)
    assert member is not None
    assert member.role == "admin"
    assert member.email == email

    # Second acceptance finds no pending invite — no duplicate member created.
    again = await invitations.accept_pending_invitation(email, org_id=org_id)
    assert again is None


@pytest.mark.asyncio
async def test_revoked_invitation_cannot_be_accepted():
    org_id, member_id, _ = await _make_member("owner")
    inviter = Member(id=member_id, organization_id=org_id, email="a@t.io", role="owner")
    email = f"rev-{uuid.uuid4().hex[:8]}@t.io"

    created = await invitations.create_invitation(inviter, email, "user")
    assert await invitations.revoke_invitation(inviter, created["id"]) is True
    assert await invitations.accept_pending_invitation(email, org_id=org_id) is None


@pytest.mark.asyncio
async def test_expired_invitation_cannot_be_accepted():
    org_id, member_id, _ = await _make_member("owner")
    inviter = Member(id=member_id, organization_id=org_id, email="a@t.io", role="owner")
    email = f"exp-{uuid.uuid4().hex[:8]}@t.io"

    created = await invitations.create_invitation(inviter, email, "user")
    table = await reflect_table("invitations")
    async with engine.begin() as conn:
        await conn.execute(
            table.update()
            .where(table.c.id == created["id"])
            .values(expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        )
    assert await invitations.accept_pending_invitation(email, org_id=org_id) is None
