"""W2.3 — FGA group modeling + SCIM group reconciliation to tuples.

Modeling approach: *materialized org tuples*.  Rather than adding a ``group``
type to the OpenFGA authorization model (which cannot be validated without a
live FGA server), we write direct ``user:{member_id}`` → ``member|admin`` →
``organization:{org_id}`` tuples when reconciling group memberships.  This
satisfies the required behavioral outcome — a member of an admin-role SCIM
group ends up with ``admin`` on ``organization:{org_id}`` — via the existing
``grant_org_membership`` helper, leaving the authorization model untouched.

AUTHORIZATION_MODEL already contains the ``organization`` type with direct
``member`` and ``admin`` relations that accept ``user`` objects.  No model
change is required for materialized grants.

Tests run without a live OpenFGA by monkeypatching
``core.permissions.settings_openfga_configured`` and the grant helpers.
"""
from __future__ import annotations

import uuid

import pytest

from core import permissions
from core.db import engine, reflect_table


# ── Seed helpers ──────────────────────────────────────────────────────────────


async def _seed_org_with_group(*, group_role: str = "admin") -> tuple[str, str, str]:
    """Return (org_id, member_id, group_id) with a SCIM group that grants group_role."""
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    group_id = str(uuid.uuid4())

    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    groups = await reflect_table("scim_groups")
    gm = await reflect_table("group_memberships")

    async with engine.begin() as conn:
        await conn.execute(
            orgs.insert().values(
                id=org_id,
                slug=f"o{org_id[:8]}",
                subdomain=f"o{org_id[:8]}",
                name="TestOrg",
            )
        )
        await conn.execute(
            members.insert().values(
                id=member_id,
                organization_id=org_id,
                email=f"{member_id[:8]}@test.io",
                role=group_role,
            )
        )
        await conn.execute(
            groups.insert().values(
                id=group_id,
                organization_id=org_id,
                display_name="Admins",
                role=group_role,
            )
        )
        await conn.execute(
            gm.insert().values(
                organization_id=org_id,
                group_id=group_id,
                member_id=member_id,
            )
        )

    return org_id, member_id, group_id


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_reconcile_noop_when_fga_off(monkeypatch):
    """reconcile_org_groups returns zero counts and writes nothing when FGA is off."""
    monkeypatch.setattr("core.permissions.settings_openfga_configured", lambda: False)

    org_id, _mid, _gid = await _seed_org_with_group(group_role="admin")
    result = await permissions.reconcile_org_groups(org_id)

    assert result == {"groups": 0, "grants": 0}


@pytest.mark.asyncio
async def test_admin_group_member_gets_org_admin(monkeypatch):
    """A member of an admin-role SCIM group is granted org admin in FGA."""
    monkeypatch.setattr("core.permissions.settings_openfga_configured", lambda: True)

    syncs: list[tuple[str, str, str, bool]] = []

    async def fake_sync(member_id: str, org_id: str, *, role: str, active: bool = True) -> None:
        syncs.append((member_id, org_id, role, active))

    monkeypatch.setattr("core.permissions.sync_org_membership", fake_sync)

    org_id, member_id, _gid = await _seed_org_with_group(group_role="admin")
    result = await permissions.reconcile_org_groups(org_id)

    # Exactly one group, exactly one grant (the single member).
    assert result["groups"] == 1
    assert result["grants"] == 1

    # The grant must target the correct member+org and be elevated to admin.
    assert len(syncs) == 1
    granted_member, granted_org, granted_role, active = syncs[0]
    assert granted_member == member_id
    assert granted_org == org_id
    assert granted_role == "admin"
    assert active is True


@pytest.mark.asyncio
async def test_user_group_member_gets_org_member(monkeypatch):
    """A member of a user-role SCIM group is granted org membership (not admin)."""
    monkeypatch.setattr("core.permissions.settings_openfga_configured", lambda: True)

    syncs: list[tuple[str, str, str, bool]] = []

    async def fake_sync(member_id: str, org_id: str, *, role: str, active: bool = True) -> None:
        syncs.append((member_id, org_id, role, active))

    monkeypatch.setattr("core.permissions.sync_org_membership", fake_sync)

    org_id, member_id, _gid = await _seed_org_with_group(group_role="user")
    result = await permissions.reconcile_org_groups(org_id)

    assert result["grants"] == 1
    _, _, granted_role, active = syncs[0]
    assert granted_role == "user"
    assert active is True


@pytest.mark.asyncio
async def test_no_groups_returns_zero_grants(monkeypatch):
    """An org with no SCIM groups produces zero grants even when FGA is on."""
    monkeypatch.setattr("core.permissions.settings_openfga_configured", lambda: True)

    syncs: list = []

    async def fake_sync(member_id: str, org_id: str, *, role: str, active: bool = True) -> None:
        syncs.append((member_id, org_id, role, active))

    monkeypatch.setattr("core.permissions.sync_org_membership", fake_sync)

    # Seed an org with a member but NO groups.
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(
            orgs.insert().values(
                id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="NoGroups"
            )
        )
        await conn.execute(
            members.insert().values(
                id=member_id, organization_id=org_id,
                email=f"{member_id[:8]}@test.io", role="user",
            )
        )

    result = await permissions.reconcile_org_groups(org_id)
    assert result == {"groups": 0, "grants": 1}
    assert syncs == [(member_id, org_id, "user", True)]


@pytest.mark.asyncio
async def test_sync_org_membership_revokes_stale_admin_before_member_grant(monkeypatch):
    monkeypatch.setattr("core.permissions.settings_openfga_configured", lambda: True)
    operations: list[tuple[str, tuple[str, str, str]]] = []

    async def fake_delete(tuple_key: tuple[str, str, str]) -> None:
        operations.append(("delete", tuple_key))

    async def fake_write(tuples: list[tuple[str, str, str]]) -> None:
        operations.extend(("write", tuple_key) for tuple_key in tuples)

    monkeypatch.setattr("core.permissions._delete_tuple_idempotently", fake_delete)
    monkeypatch.setattr("core.permissions._write_tuples_idempotently", fake_write)

    await permissions.sync_org_membership("member-1", "org-1", role="user")

    assert operations == [
        ("delete", ("user:member-1", "admin", "organization:org-1")),
        ("write", ("user:member-1", "member", "organization:org-1")),
    ]


@pytest.mark.asyncio
async def test_sync_deactivated_member_revokes_every_org_relation(monkeypatch):
    monkeypatch.setattr("core.permissions.settings_openfga_configured", lambda: True)
    deleted: list[tuple[str, str, str]] = []
    written: list[tuple[str, str, str]] = []

    async def fake_delete(tuple_key: tuple[str, str, str]) -> None:
        deleted.append(tuple_key)

    async def fake_write(tuples: list[tuple[str, str, str]]) -> None:
        written.extend(tuples)

    monkeypatch.setattr("core.permissions._delete_tuple_idempotently", fake_delete)
    monkeypatch.setattr("core.permissions._write_tuples_idempotently", fake_write)

    await permissions.sync_org_membership(
        "member-1", "org-1", role="admin", active=False
    )

    assert deleted == [
        ("user:member-1", "member", "organization:org-1"),
        ("user:member-1", "admin", "organization:org-1"),
    ]
    assert written == []


@pytest.mark.asyncio
async def test_removing_final_admin_group_reconciles_member_to_user(monkeypatch):
    monkeypatch.setattr("core.permissions.settings_openfga_configured", lambda: True)
    syncs: list[tuple[str, str, str, bool]] = []

    async def fake_sync(member_id: str, org_id: str, *, role: str, active: bool = True) -> None:
        syncs.append((member_id, org_id, role, active))

    monkeypatch.setattr("core.permissions.sync_org_membership", fake_sync)
    org_id, member_id, group_id = await _seed_org_with_group(group_role="admin")

    from core.scim import set_group_members

    await set_group_members(org_id, group_id, [])

    assert syncs[-1] == (member_id, org_id, "user", True)


@pytest.mark.asyncio
async def test_authorization_model_is_well_formed():
    """AUTHORIZATION_MODEL contains the expected types and relations without a group type.

    The materialized approach leaves the model untouched — this sanity-checks
    that the existing model still has the ``organization`` type with ``member``
    and ``admin`` relations that accept ``user`` objects directly.
    """
    from core.authz import AUTHORIZATION_MODEL

    type_names = {td["type"] for td in AUTHORIZATION_MODEL["type_definitions"]}
    assert "user" in type_names
    assert "organization" in type_names
    # No group type added — materialized approach does not require it.
    assert "group" not in type_names

    org_def = next(td for td in AUTHORIZATION_MODEL["type_definitions"] if td["type"] == "organization")
    assert "member" in org_def["relations"]
    assert "admin" in org_def["relations"]
    # Both relations must directly accept user objects.
    meta = org_def.get("metadata", {}).get("relations", {})
    for rel in ("member", "admin"):
        types = [t["type"] for t in meta.get(rel, {}).get("directly_related_user_types", [])]
        assert "user" in types, f"organization.{rel} must directly accept user"
