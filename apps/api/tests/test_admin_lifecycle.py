from __future__ import annotations

from pathlib import Path

import pytest


def test_org_api_key_material_uses_lookup_id_and_vault_derived_digest(monkeypatch):
    from core import organization_api_keys as api_keys

    monkeypatch.setattr(api_keys.settings, "vault_encryption_key", "11" * 32)
    lookup_id, plaintext, prefix, digest = api_keys._new_material()

    assert plaintext.startswith(f"chr_live_{lookup_id}_")
    assert api_keys._parse_lookup_id(plaintext) == lookup_id
    assert plaintext not in digest
    assert len(digest) == 64
    assert prefix.endswith("…")
    assert api_keys._parse_lookup_id(f"chr_live_{lookup_id}_tampered") == lookup_id
    assert api_keys._digest(plaintext, purpose="org-api-key") == digest
    assert api_keys._digest(f"{plaintext}x", purpose="org-api-key") != digest


@pytest.mark.parametrize(
    "token",
    ["", "chr_live_", "chr_live_not-hex_secret", "chr_live_0123456789abcdef0123_"],  # gitleaks:allow - intentionally malformed test tokens
)
def test_org_api_key_parser_rejects_malformed_tokens(token: str):
    from core.organization_api_keys import _parse_lookup_id

    assert _parse_lookup_id(token) is None


def test_org_api_key_plaintext_is_covered_by_canonical_audit_redaction(monkeypatch):
    from core import organization_api_keys as api_keys
    from core.audit_redaction import REDACTED, redact

    monkeypatch.setattr(api_keys.settings, "vault_encryption_key", "11" * 32)
    _, plaintext, _, _ = api_keys._new_material()

    assert redact({"accidental_value": plaintext, "plaintext_key": plaintext}) == {
        "accidental_value": REDACTED,
        "plaintext_key": REDACTED,
    }


def test_org_api_key_digest_survives_jwt_rotation_but_not_vault_rotation(monkeypatch):
    from core import organization_api_keys as api_keys

    monkeypatch.setattr(api_keys.settings, "vault_encryption_key", "22" * 32)
    monkeypatch.setattr(api_keys.settings, "jwt_secret", "jwt-before")
    before = api_keys._digest("chr_live_example", purpose="org-api-key")
    monkeypatch.setattr(api_keys.settings, "jwt_secret", "jwt-after")
    assert api_keys._digest("chr_live_example", purpose="org-api-key") == before
    monkeypatch.setattr(api_keys.settings, "vault_encryption_key", "33" * 32)
    assert api_keys._digest("chr_live_example", purpose="org-api-key") != before


@pytest.mark.asyncio
async def test_api_key_rotation_rolls_back_replacement_if_revoke_crashes(monkeypatch):
    from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table
    from sqlalchemy.dialects.postgresql import ARRAY
    from sqlalchemy.sql.dml import Insert, Update
    from sqlalchemy.sql.selectable import Select

    from core import organization_api_keys as api_keys
    from core.models import Member

    table = Table(
        "organization_api_keys",
        MetaData(),
        Column("id", String),
        Column("organization_id", String),
        Column("region", String),
        Column("name", String),
        Column("lookup_id", String),
        Column("key_prefix", String),
        Column("secret_hash", String),
        Column("scopes", ARRAY(String)),
        Column("rate_limit_per_minute", Integer),
        Column("status", String),
        Column("expires_at", DateTime(timezone=True)),
        Column("last_used_at", DateTime(timezone=True)),
        Column("created_by_member_id", String),
        Column("rotated_from_id", String),
        Column("created_at", DateTime(timezone=True)),
        Column("revoked_at", DateTime(timezone=True)),
    )
    current = {
        "id": "key-old",
        "organization_id": "org-a",
        "region": "us",
        "name": "automation",
        "scopes": ["write"],
        "rate_limit_per_minute": 60,
        "status": "active",
        "expires_at": None,
    }

    class Result:
        def __init__(self, row=None):
            self.row = row

        def mappings(self):
            return self

        def first(self):
            return self.row

        def one(self):
            return self.row

    class Connection:
        async def execute(self, statement):
            if isinstance(statement, Select):
                return Result(current)
            if isinstance(statement, Insert):
                fake_engine.replacement_staged = True
                return Result({**current, "id": "key-new", "key_prefix": "chr_live_new…"})
            if isinstance(statement, Update):
                raise ConnectionError("simulated connection loss before predecessor revoke")
            raise AssertionError(type(statement))

    class Transaction:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, _exc, _tb):
            if exc_type is not None:
                fake_engine.replacement_staged = False
                fake_engine.rolled_back = True
            return False

    class Engine:
        replacement_staged = False
        rolled_back = False

        def begin(self):
            return Transaction()

    fake_engine = Engine()
    async def reflected(_name):
        return table

    monkeypatch.setattr(api_keys, "engine", fake_engine)
    monkeypatch.setattr(api_keys, "reflect_table", reflected)
    monkeypatch.setattr(api_keys, "_new_material", lambda: ("a" * 20, "plaintext", "prefix", "digest"))

    with pytest.raises(ConnectionError):
        await api_keys.rotate_key(
            Member(id="owner", organization_id="org-a", email="owner@example.com", role="owner"),
            "key-old",
        )

    assert fake_engine.rolled_back is True
    assert fake_engine.replacement_staged is False


@pytest.mark.asyncio
async def test_api_key_admin_scope_is_required_for_admin_lifecycle(monkeypatch):
    from core import permissions
    from core.exceptions import PermissionDenied
    from core.models import Member

    async def allow_role_policy(*_args, **_kwargs):
        return True

    async def no_audit(*_args, **_kwargs):
        return "audit-id"

    monkeypatch.setattr(permissions, "_role_policy_allows", allow_role_policy)
    monkeypatch.setattr(permissions.audit, "log", no_audit)
    actor = Member(
        id="owner-1",
        organization_id="org-a",
        email="owner@example.com",
        role="owner",
        auth_type="api_key",
        api_key_id="key-1",
        api_key_scopes=["read"],
    )

    with pytest.raises(PermissionDenied):
        await permissions.check(actor, "manage_workspaces", "org-a")

    actor.api_key_scopes = ["admin"]
    assert await permissions.check(actor, "manage_workspaces", "org-a") is True


def test_admin_lifecycle_migration_and_leader_job_are_linear_and_retention_safe():
    root = Path(__file__).parents[1]
    migration = (root / "migrations/versions/0061_admin_lifecycle.py").read_text()
    main = (root / "main.py").read_text()
    job = (root / "jobs/admin_lifecycle.py").read_text()

    assert 'down_revision = "0060_task_cleanup"' in migration
    assert '"native_groups"' in migration
    assert '"workspaces"' in migration
    assert '"legacy_key"' in migration
    assert "INSERT INTO workspace_members" in migration
    assert '"workspace_members"' in migration
    assert '"organization_api_keys"' in migration
    assert "deletion_execute_after" in migration
    assert "'workspace'" in migration
    assert "admin_lifecycle_jobs.scheduler" in main
    assert "process_due_workspace_deletions" in job


def test_workspace_and_organization_owner_guards_are_present_on_locked_rows():
    root = Path(__file__).parents[1]
    lifecycle = (root / "core/admin_lifecycle.py").read_text()

    assert lifecycle.count("with_for_update()") >= 5
    assert "A workspace must keep at least one owner" in lifecycle
    assert "Transfer ownership before the last owner leaves" in lifecycle
    assert 'members.c.organization_id == actor.organization_id' in lifecycle
    assert 'workspaces.c.organization_id == actor.organization_id' in lifecycle
    assert "_org_retention_lock" in lifecycle
    assert 'payload={"mode": "retained_tombstone"}' in lifecycle


def test_workspace_lifecycle_is_bound_to_creation_and_runtime_paths():
    root = Path(__file__).parents[1]
    permissions = (root / "core/permissions.py").read_text()
    chat = (root / "routers/chat.py").read_text()
    workflow_runtime = (root / "connectors/framework/workflows.py").read_text()

    for action in (
        "create_task",
        "create_workflow",
        "create_connector_plan",
        "execute_connector_plan",
        "execute_connector_tool_call",
    ):
        assert f'"{action}"' in permissions
    assert "require_workspace_access" in permissions
    assert "req.workspace_id or \"default\"" in chat
    assert "_require_live_workspace" in workflow_runtime
    assert "start_run" in workflow_runtime


def test_settings_ui_replaces_disabled_lifecycle_controls_with_real_endpoints():
    repo_root = Path(__file__).parents[3]
    page = (repo_root / "apps/web/app/chat/page.tsx").read_text()
    lifecycle_ui = (
        repo_root / "apps/web/components/settings/AdminLifecycleSettings.tsx"
    ).read_text()

    assert "OrganizationApiKeysSettings" in page
    assert "OrganizationDangerSettings" in page
    assert "AdminDirectorySettings" in page
    assert "plaintext_key" in lifecycle_ui
    assert "I saved it" in lifecycle_ui
    assert "/settings/admin-lifecycle/ownership-transfer" in lifecycle_ui
    assert "/settings/admin-lifecycle/leave" in lifecycle_ui
    assert "/settings/admin-lifecycle/workspaces/" in lifecycle_ui
    assert "Leaving the only local workspace is not supported" not in page
    assert "Ownership transfer is not implemented" not in page
