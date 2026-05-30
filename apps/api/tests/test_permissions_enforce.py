"""Permission seam enforcement tests.

Enforcement is off by default (Phase-1 allow-all). When enabled it maps
project actions to OpenFGA relations and RAISES PermissionDenied on a deny —
no call site reads the bool, so raising is the only real block.
"""
import pytest
from unittest.mock import AsyncMock

from core import permissions
from core.exceptions import PermissionDenied
from core.models import Member


def _member(member_id="alice", role="user"):
    return Member(id=member_id, organization_id="default", email="a@x.com", role=role)


@pytest.fixture(autouse=True)
def _silence_audit(monkeypatch):
    monkeypatch.setattr(permissions.audit, "log", AsyncMock())


@pytest.mark.asyncio
async def test_disabled_is_allow_all_and_never_calls_authz(monkeypatch):
    monkeypatch.setattr(permissions.authz, "is_enabled", lambda: False)
    check = AsyncMock()
    monkeypatch.setattr(permissions.authz, "check", check)

    assert await permissions.check(_member(), "delete_project_source", "p1") is True
    assert check.await_count == 0


@pytest.mark.asyncio
async def test_enabled_allows_when_model_grants(monkeypatch):
    monkeypatch.setattr(permissions.authz, "is_enabled", lambda: True)
    monkeypatch.setattr(permissions.authz, "check", AsyncMock(return_value=True))

    assert await permissions.check(_member(), "view_project_sources", "p1") is True


@pytest.mark.asyncio
async def test_enabled_denies_with_raise(monkeypatch):
    monkeypatch.setattr(permissions.authz, "is_enabled", lambda: True)
    check = AsyncMock(return_value=False)
    monkeypatch.setattr(permissions.authz, "check", check)

    with pytest.raises(PermissionDenied):
        await permissions.check(_member(), "delete_project_source", "p1")
    # Mapped to the manage relation on the project object.
    user, relation, obj = check.await_args.args
    assert (user, relation, obj) == ("user:alice", "can_manage", "project:p1")


@pytest.mark.asyncio
async def test_enabled_fails_closed_when_authz_unavailable(monkeypatch):
    from core.authz import AuthzUnavailable

    monkeypatch.setattr(permissions.authz, "is_enabled", lambda: True)
    monkeypatch.setattr(permissions.authz, "check", AsyncMock(side_effect=AuthzUnavailable("down")))

    with pytest.raises(PermissionDenied):
        await permissions.check(_member(), "view_project_sources", "p1")


@pytest.mark.asyncio
async def test_internal_actor_bypasses_enforcement(monkeypatch):
    monkeypatch.setattr(permissions.authz, "is_enabled", lambda: True)
    check = AsyncMock(return_value=False)
    monkeypatch.setattr(permissions.authz, "check", check)

    # The agent runtime / sync jobs are internal; broker safety limits still apply.
    assert await permissions.check(_member(role="agent"), "delete_project_source", "p1") is True
    assert await permissions.check(_member(member_id="source_sync"), "add_project_source", "p1") is True
    assert check.await_count == 0


@pytest.mark.asyncio
async def test_unmapped_action_allowed_even_when_enabled(monkeypatch):
    monkeypatch.setattr(permissions.authz, "is_enabled", lambda: True)
    check = AsyncMock(return_value=False)
    monkeypatch.setattr(permissions.authz, "check", check)

    assert await permissions.check(_member(), "use_tool:gmail.search", "default") is True
    assert check.await_count == 0


@pytest.mark.asyncio
async def test_grant_project_role_writes_member_and_org_tuples(monkeypatch):
    monkeypatch.setattr(permissions, "settings_openfga_configured", lambda: True)
    write = AsyncMock()
    monkeypatch.setattr(permissions.authz, "write_tuples", write)

    await permissions.grant_project_role("bob", "owner", "p1", "default")
    tuples = write.await_args.args[0]
    assert ("user:bob", "owner", "project:p1") in tuples
    assert ("organization:default", "org", "project:p1") in tuples
