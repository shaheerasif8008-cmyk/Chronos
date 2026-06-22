"""W2.1 — prove the OpenFGA-enabled relationship path.

With ``openfga_api_url`` empty (the default and the standard test env) every
mapped action returns ``granted_stub`` and the relationship checks are never
exercised. These tests turn enforcement ON against a *live* OpenFGA server and
prove the moat's unproven half:

  * an actor with no relationship tuple is DENIED a mapped action,
  * granting the tuple then ALLOWS it,
  * an org admin inherits resource access via the org→resource link,
  * a mapped action FAILS CLOSED when the server is unreachable.

The live-server tests skip gracefully when no OpenFGA is reachable (so the
default suite stays green); CI provisions OpenFGA and runs them. The
fail-closed test needs no server and always runs.

The server location is read from ``OPENFGA_TEST_URL`` (default
``http://localhost:8080``) — deliberately a different name from the
``OPENFGA_API_URL`` that feeds ``Settings``, so pointing the tests at a server
does not switch enforcement on for the rest of the suite. Each test enables
enforcement only within its own fixture scope (monkeypatch) and uses unique
resource ids so tuples never collide across runs.
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


def _member(role: str = "user") -> Member:
    return Member(
        id=f"u-{uuid.uuid4()}",
        organization_id="default",
        email="x@example.com",
        role=role,
    )


@pytest.fixture
def enforced_fga(monkeypatch):
    """Enable OpenFGA enforcement against the live server for one test.

    Skips when no server is reachable. Stubs ``audit.log`` so the test is
    DB-independent and yields the captured decision list for assertions.
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


@pytest.mark.asyncio
async def test_unauthorized_member_denied_project_edit(enforced_fga):
    """No can_edit tuple on the project → update_project is denied."""
    project_id = f"proj-{uuid.uuid4()}"
    member = _member()

    with pytest.raises(PermissionDenied):
        await permissions.check(member, "update_project", project_id)
    assert enforced_fga[-1] == "denied"


@pytest.mark.asyncio
async def test_grant_then_allowed_project_edit(enforced_fga):
    """Granting an editor tuple flips the same check from deny to allow."""
    project_id = f"proj-{uuid.uuid4()}"
    member = _member()

    with pytest.raises(PermissionDenied):
        await permissions.check(member, "update_project", project_id)

    await permissions.grant_project_role(member.id, "editor", project_id, "default")

    assert await permissions.check(member, "update_project", project_id) is True
    assert enforced_fga[-1] == "granted"


@pytest.mark.asyncio
async def test_unauthorized_member_denied_workspace_view(enforced_fga):
    """No can_view tuple on the workspace → view_workspace is denied."""
    workspace_id = f"ws-{uuid.uuid4()}"
    member = _member()

    with pytest.raises(PermissionDenied):
        await permissions.check(member, "view_workspace", workspace_id)
    assert enforced_fga[-1] == "denied"


@pytest.mark.asyncio
async def test_org_admin_inherits_project_manage(enforced_fga):
    """An org admin can_manage any project linked to the org, with no per-project
    tuple — proves the org→resource inheritance edge in the model."""
    project_id = f"proj-{uuid.uuid4()}"
    # Seed the project→org link (a side effect of granting any project role).
    await permissions.grant_project_role(
        f"u-{uuid.uuid4()}", "editor", project_id, "default"
    )

    admin = _member()  # role="user" so this is decided by FGA, not the role gate
    await permissions.grant_org_membership(admin.id, "default", admin=True)

    assert await permissions.check(admin, "delete_project", project_id) is True
    assert enforced_fga[-1] == "granted"


@pytest.mark.asyncio
async def test_mapped_action_fails_closed_when_server_unreachable(monkeypatch):
    """Enforcement on + server down → a mapped action fails CLOSED.

    Needs no live server (points at an unreachable address), so it always runs.
    """
    monkeypatch.setattr(authz.settings, "openfga_api_url", "http://127.0.0.1:1")
    monkeypatch.setattr(authz.settings, "permissions_enforce", True)
    monkeypatch.setattr(authz.settings, "openfga_store_id", "")
    monkeypatch.setattr(authz.settings, "openfga_model_id", "")

    decisions: list[str | None] = []

    async def _capture(event_type, actor_id, action, **kw):
        decisions.append(kw.get("decision"))

    monkeypatch.setattr(permissions.audit, "log", _capture)
    authz.reset_cache()
    try:
        member = _member()
        with pytest.raises(PermissionDenied):
            await permissions.check(member, "update_project", "p1")
        assert decisions[-1] == "denied_authz_unavailable"
    finally:
        authz.reset_cache()
