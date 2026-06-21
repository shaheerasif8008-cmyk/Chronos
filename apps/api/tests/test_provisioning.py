"""W1 Phase 2A — parameterized org provisioning."""
from __future__ import annotations

import uuid
import pytest
from sqlalchemy import select

from core.db import engine, reflect_table
import core.provisioning as provisioning
from core.provisioning import provision_org


@pytest.mark.asyncio
async def test_provision_org_creates_org_owner_and_context():
    slug = f"acme{uuid.uuid4().hex[:8]}"
    result = await provision_org(slug=slug, name="Acme Inc", owner_email="Founder@Acme.com")
    org_id, owner_id = result["org_id"], result["owner_member_id"]

    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        org = (await conn.execute(select(orgs).where(orgs.c.id == org_id))).mappings().one()
        owner = (await conn.execute(select(members).where(members.c.id == owner_id))).mappings().one()

    assert org["slug"] == slug
    assert org["subdomain"] == slug
    assert org["organization_id"] == org_id
    assert org["owner_member_id"] == owner_id
    assert owner["organization_id"] == org_id
    assert owner["role"] == "owner"
    assert owner["email"] == "founder@acme.com"
    # Context folder written under the (monkeypatched) provisioning.ROOT.
    assert (provisioning.ROOT / "context" / org_id / "org.md").exists()

    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        rows = (await conn.execute(
            select(audit_log).where(
                audit_log.c.event_type == "org_provisioned",
                audit_log.c.resource_id == org_id,
            )
        )).mappings().all()
    assert len(rows) == 1 and rows[0]["organization_id"] == org_id
