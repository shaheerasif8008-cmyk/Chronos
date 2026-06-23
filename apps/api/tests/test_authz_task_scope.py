"""W2.2 — task resource scoping via OpenFGA.

With ``openfga_api_url`` empty (the default test env) all mapped task actions
return ``granted_stub`` and relationship checks are never exercised.

The FGA-enabled tests (test_task_owner_*, test_non_owner_denied_*, etc.) skip
gracefully when no OpenFGA server is reachable at OPENFGA_TEST_URL, so the
default suite stays green.

The unit-level tests (test_resource_for_*) require no server and always run.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest

from core import authz, permissions
from core.exceptions import PermissionDenied
from core.models import Member

OPENFGA_URL = os.environ.get("OPENFGA_TEST_URL", "http://localhost:8080")


def _openfga_reachable(url: str) -> bool:
    try:
        resp = httpx.get(f"{url.rstrip('/')}/stores", timeout=2.0)
    except httpx.HTTPError:
        return False
    return resp.status_code < 500


def _member(role: str = "user", org_id: str = "default") -> Member:
    return Member(
        id=f"u-{uuid.uuid4()}",
        organization_id=org_id,
        email="x@example.com",
        role=role,
    )


@pytest.fixture
def enforced_fga(monkeypatch):
    """Enable OpenFGA enforcement against the live server for one test.

    Skips when no server is reachable at OPENFGA_TEST_URL. Stubs ``audit.log``
    so the test is DB-independent, and yields the captured decision list.
    """
    if not _openfga_reachable(OPENFGA_URL):
        pytest.skip(f"OpenFGA not reachable at {OPENFGA_URL}")

    monkeypatch.setattr(authz.settings, "openfga_api_url", OPENFGA_URL)
    monkeypatch.setattr(authz.settings, "permissions_enforce", True)
    monkeypatch.setattr(authz.settings, "openfga_store_id", "")
    monkeypatch.setattr(authz.settings, "openfga_model_id", "")

    decisions: list[str | None] = []

    async def _capture(event_type, actor_id, action, **kw):
        decisions.append(kw.get("decision"))

    monkeypatch.setattr(permissions.audit, "log", _capture)

    authz.reset_cache()
    yield decisions
    authz.reset_cache()


# ── Unit tests (no server required) ─────────────────────────────────────────


def test_resource_for_cancel_task_returns_can_manage_task():
    """_resource_for maps cancel_task to (can_manage, task)."""
    assert permissions._resource_for("cancel_task") == ("can_manage", "task")


def test_resource_for_retry_task_returns_can_manage_task():
    assert permissions._resource_for("retry_task") == ("can_manage", "task")


def test_resource_for_view_task_returns_can_view_task():
    assert permissions._resource_for("view_task") == ("can_view", "task")


def test_resource_for_view_task_events_returns_can_view_task():
    assert permissions._resource_for("view_task_events") == ("can_view", "task")


def test_resource_for_stream_task_returns_can_view_task():
    assert permissions._resource_for("stream_task") == ("can_view", "task")


# ── FGA-enabled tests (skip when server is down) ─────────────────────────────


@pytest.mark.asyncio
async def test_task_owner_can_cancel_task(enforced_fga):
    """An owner tuple on the task allows cancel_task (can_manage check)."""
    task_id = f"task-{uuid.uuid4()}"
    org_id = "default"
    owner = _member(org_id=org_id)

    # No tuple yet → denied.
    with pytest.raises(PermissionDenied):
        await permissions.check(owner, "cancel_task", task_id)

    # Grant owner role (also writes org→task link).
    await permissions.grant_task_role(owner.id, "owner", task_id, org_id)

    # Now allowed.
    assert await permissions.check(owner, "cancel_task", task_id) is True
    assert enforced_fga[-1] == "granted"


@pytest.mark.asyncio
async def test_non_owner_denied_cancel_task(enforced_fga):
    """A member with no task tuple cannot cancel the task."""
    task_id = f"task-{uuid.uuid4()}"
    org_id = "default"

    # Seed an owner so the org→task link exists (proves denial is per-member).
    await permissions.grant_task_role(f"u-{uuid.uuid4()}", "owner", task_id, org_id)

    non_owner = _member(org_id=org_id)  # a different member, no tuple

    with pytest.raises(PermissionDenied):
        await permissions.check(non_owner, "cancel_task", task_id)
    assert enforced_fga[-1] == "denied"


@pytest.mark.asyncio
async def test_org_admin_inherits_task_manage(enforced_fga):
    """An org admin can_manage any task linked to the org — proves org→task inheritance."""
    task_id = f"task-{uuid.uuid4()}"
    org_id = "default"

    # Write the org→task link (side-effect of granting any task role).
    await permissions.grant_task_role(f"u-{uuid.uuid4()}", "owner", task_id, org_id)

    admin = _member(org_id=org_id)  # role="user" so the deterministic gate does NOT block
    await permissions.grant_org_membership(admin.id, org_id, admin=True)

    assert await permissions.check(admin, "cancel_task", task_id) is True
    assert enforced_fga[-1] == "granted"


@pytest.mark.asyncio
async def test_task_owner_can_view_task(enforced_fga):
    """Owner can also view the task (can_view is a superset of can_manage for owners)."""
    task_id = f"task-{uuid.uuid4()}"
    org_id = "default"
    owner = _member(org_id=org_id)

    await permissions.grant_task_role(owner.id, "owner", task_id, org_id)

    assert await permissions.check(owner, "view_task", task_id) is True
    assert enforced_fga[-1] == "granted"
