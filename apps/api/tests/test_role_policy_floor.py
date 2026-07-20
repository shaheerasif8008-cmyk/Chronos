"""Deterministic human-role floor for generic production actions."""

from unittest.mock import AsyncMock

import pytest

from core import permissions
from core.exceptions import PermissionDenied
from core.models import Member


def _member(role: str) -> Member:
    return Member(
        id=f"{role}-member",
        organization_id="org-1",
        email=f"{role}@example.com",
        role=role,
    )


@pytest.fixture(autouse=True)
def _quiet_audit(monkeypatch):
    monkeypatch.setattr(permissions.audit, "log", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "use_tool:browser.search",
        "create_browser_session",
        "list_memory",
        "list_approvals",
        "create_agent",
        "artifact.publish",
    ],
)
async def test_viewer_cannot_cross_read_only_role_floor(action):
    with pytest.raises(PermissionDenied):
        await permissions.check(_member("viewer"), action, "resource-1")


@pytest.mark.asyncio
async def test_legacy_user_maps_to_operator_for_tools_but_not_employee_creation():
    assert await permissions.check(_member("user"), "use_tool:browser.search", "workspace-1")
    with pytest.raises(PermissionDenied):
        await permissions.check(_member("user"), "create_agent", "org-1")


@pytest.mark.asyncio
async def test_manager_can_create_employee_but_default_workspace_policy_still_denies_routines():
    assert await permissions.check(_member("manager"), "create_agent", "org-1")
    with pytest.raises(PermissionDenied):
        await permissions.check(_member("manager"), "create_schedule", "org-1")


@pytest.mark.asyncio
async def test_admin_can_perform_publication_and_workspace_mutations():
    admin = _member("admin")
    assert await permissions.check(admin, "artifact.publish", "artifact-1")
    assert await permissions.check(admin, "create_schedule", "org-1")


@pytest.mark.asyncio
async def test_saved_role_matrix_can_further_restrict_but_not_lower_absolute_floor(monkeypatch):
    async def deny_employee(*_args, **_kwargs):
        return {
            "roles": {
                "manager": {
                    "workspace": "deny",
                    "employee": "deny",
                    "tools": "approval_required",
                    "approvals": "allow",
                    "memory": "allow",
                    "audit": "deny",
                }
            }
        }

    monkeypatch.setattr("core.settings_store.get_settings_doc", deny_employee)
    with pytest.raises(PermissionDenied):
        await permissions.check(_member("manager"), "create_agent", "org-1")

    # Even a permissive stored document cannot grant a viewer a manager action.
    async def allow_everything(*_args, **_kwargs):
        return {"roles": {"viewer": {key: "allow" for key in (
            "workspace", "employee", "tools", "approvals", "memory", "audit"
        )}}}

    monkeypatch.setattr("core.settings_store.get_settings_doc", allow_everything)
    with pytest.raises(PermissionDenied):
        await permissions.check(_member("viewer"), "create_agent", "org-1")
