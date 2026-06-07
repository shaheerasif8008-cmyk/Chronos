from __future__ import annotations

import shutil
import uuid

import pytest
from sqlalchemy import delete, insert, select


@pytest.mark.asyncio
async def test_seed_promotes_existing_admin_email_to_admin(monkeypatch):
    from core.db import engine, reflect_table
    import seed

    org_id = f"seed-test-{uuid.uuid4().hex[:8]}"
    admin_email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    context_dir = seed.ROOT / "context" / org_id

    monkeypatch.setattr(seed.settings, "org_id", org_id)
    monkeypatch.setattr(seed.settings, "admin_email", admin_email)

    async with engine.begin() as conn:
        await conn.execute(insert(orgs).values(id=org_id, slug=org_id, name="Seed Test Org"))
        await conn.execute(
            insert(members).values(
                organization_id=org_id,
                email=admin_email,
                role="user",
                name="Existing Admin",
            )
        )

    try:
        await seed.upsert_seed()

        async with engine.begin() as conn:
            role = (
                await conn.execute(
                    select(members.c.role).where(
                        members.c.organization_id == org_id,
                        members.c.email == admin_email,
                    )
                )
            ).scalar_one()

        assert role == "admin"
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(members).where(members.c.organization_id == org_id))
            await conn.execute(delete(orgs).where(orgs.c.id == org_id))
        shutil.rmtree(context_dir, ignore_errors=True)
