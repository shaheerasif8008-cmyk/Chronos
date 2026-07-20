"""Permission seam enforcement tests.

Focus on the always-on, policy-engine-independent guarantees ("enforce by
default") plus the FGA resource mapping. audit.log is stubbed so these run
without a database.
"""
from __future__ import annotations

import pytest

from core import permissions
from core.exceptions import PermissionDenied
from core.models import Member


@pytest.fixture(autouse=True)
def _no_db_audit(monkeypatch):
    async def _noop(*a, **k):
        return "audit-id"
    async def _allow_workspace(*a, **k):
        return {"id": "res", "status": "active"}
    monkeypatch.setattr(permissions.audit, "log", _noop)
    monkeypatch.setattr(
        "core.workspace_access.require_workspace_access", _allow_workspace
    )
    # Real is_enabled() is False by default (openfga_api_url empty), so only the
    # deterministic role gates run unless a test opts into FGA explicitly.


def _member(role: str) -> Member:
    return Member(id="m1", organization_id="default", email="m@x.com", role=role)


# --- Always-on admin governance gate (no OpenFGA needed) ------------------

@pytest.mark.parametrize("action", sorted(permissions._ADMIN_ACTIONS))
@pytest.mark.asyncio
async def test_admin_action_denied_for_non_admin(action):
    with pytest.raises(PermissionDenied):
        await permissions.check(_member("user"), action, "res")


@pytest.mark.parametrize("action", sorted(permissions._ADMIN_ACTIONS))
@pytest.mark.asyncio
async def test_admin_action_allowed_for_admin(action):
    assert await permissions.check(_member("admin"), action, "res") is True
    assert await permissions.check(_member("owner"), action, "res") is True


@pytest.mark.asyncio
async def test_admin_action_allowed_for_internal_actor():
    # The agent runtime / schedulers must still be able to act.
    assert await permissions.check(_member("agent"), "graduate_autonomy", "res") is True


@pytest.mark.asyncio
async def test_approval_decision_still_gated():
    with pytest.raises(PermissionDenied):
        await permissions.check(_member("user"), "decide_approval", "a1")
    assert await permissions.check(_member("approver"), "decide_approval", "a1") is True


# --- FGA resource mapping -------------------------------------------------

def test_resource_mapping_covers_project_and_workspace():
    assert permissions._resource_for("view_project") == ("can_view", "project")
    assert permissions._resource_for("delete_project") == ("can_manage", "project")
    assert permissions._resource_for("view_workspace") == ("can_view", "workspace")
    assert permissions._resource_for("update_workspace") == ("can_edit", "workspace")
    assert permissions._resource_for("delete_workspace") == ("can_manage", "workspace")
    assert permissions._resource_for("use_tool:gmail.send") is None


# --- Enforce-by-default wiring -------------------------------------------

@pytest.mark.asyncio
async def test_workspace_action_checked_against_fga_when_enabled(monkeypatch):
    seen = {}

    async def fake_check(user, relation, obj):
        seen.update(user=user, relation=relation, obj=obj)
        return False  # deny

    monkeypatch.setattr(permissions.authz, "is_enabled", lambda: True)
    monkeypatch.setattr(permissions.authz, "check", fake_check)

    with pytest.raises(PermissionDenied):
        await permissions.check(_member("user"), "delete_workspace", "ws-7")

    assert seen == {"user": "user:m1", "relation": "can_manage", "obj": "workspace:ws-7"}


@pytest.mark.asyncio
async def test_unmapped_action_allowed(monkeypatch):
    monkeypatch.setattr(permissions.authz, "is_enabled", lambda: True)
    # use_tool:* is unmapped → allowed (FGA targets project/workspace resources).
    assert await permissions.check(_member("user"), "use_tool:gmail.draft", "ws") is True


def test_enforcement_on_by_default_when_fga_configured(monkeypatch):
    # is_enabled() reflects openfga config with the default-on flag.
    monkeypatch.setattr(permissions.authz.settings, "permissions_enforce", True)
    monkeypatch.setattr(permissions.authz.settings, "openfga_api_url", "http://fga")
    assert permissions.authz.is_enabled() is True
    monkeypatch.setattr(permissions.authz.settings, "openfga_api_url", "")
    assert permissions.authz.is_enabled() is False
