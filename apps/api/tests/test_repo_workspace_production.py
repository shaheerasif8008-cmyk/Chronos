from __future__ import annotations

import json
import shlex
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete

from connectors.e2b_runtime import SANDBOX_ROOT, SandboxExpired
from core.config import settings
from core.db import engine, reflect_table


class FakeRepoRuntime:
    def __init__(self, *, expired: set[str] | None = None) -> None:
        self.expired = expired or set()
        self.files: dict[tuple[str, str], bytes] = {}
        self.commands: list[str] = []
        self.created: list[dict] = []
        self.removed: list[str] = []
        self.keepalives: list[str] = []
        self.actions: list[str] = []

    async def create(self, *, timeout_seconds, metadata):
        self.created.append({"timeout_seconds": timeout_seconds, "metadata": metadata})
        return f"sandbox-{len(self.created)}"

    async def keepalive(self, sandbox_id, *, timeout_seconds):
        self.keepalives.append(sandbox_id)
        if sandbox_id in self.expired:
            raise SandboxExpired(sandbox_id)

    async def run(self, sandbox_id, command, *, cwd, timeout_seconds):
        self.commands.append(command)
        if command.startswith("mkdir -p "):
            return {"status": "success", "returncode": 0, "stdout": "", "stderr": ""}
        tokens = shlex.split(command)
        spec_path, output_path = tokens[-2:]
        spec = json.loads(self.files[(sandbox_id, spec_path)].decode())
        action = spec["action"]
        self.actions.append(action)
        if action == "clone":
            data = {
                "branch": "main",
                "sha": "a" * 40,
                "files": ["README.md", "src/app.py"],
            }
        elif action == "archive":
            archive = f"{SANDBOX_ROOT}/{spec['archive_path']}"
            self.files[(sandbox_id, archive)] = b"durable-repo-snapshot"
            data = {"bytes": len(b"durable-repo-snapshot")}
        elif action == "restore":
            data = {"restored": True}
        elif action == "status":
            data = {"branch": "main", "dirty": False, "changes": []}
        else:
            data = {}
        self.files[(sandbox_id, output_path)] = json.dumps({"ok": True, "data": data}).encode()
        return {"status": "success", "returncode": 0, "stdout": "", "stderr": ""}

    async def write(self, sandbox_id, path, content):
        self.files[(sandbox_id, path)] = bytes(content)

    async def read(self, sandbox_id, path):
        return self.files[(sandbox_id, path)]

    async def list(self, sandbox_id, path):
        return []

    async def remove(self, sandbox_id, path):
        self.removed.append(path)
        self.files.pop((sandbox_id, path), None)

    async def kill(self, sandbox_id):
        raise AssertionError("persistent repo sandboxes are not killed after each operation")


def _workspace(*, sandbox_id: str | None = None, snapshot: str | None = None) -> dict:
    return {
        "id": "workspace-1",
        "organization_id": "org-a",
        "region": "us",
        "task_id": "task-a",
        "member_id": "member-a",
        "repo_path": "repos/imported",
        "runtime_provider": "e2b",
        "sandbox_id": sandbox_id,
        "status": "active" if sandbox_id else "creating",
        "source_type": None,
        "source_url": None,
        "snapshot_object_key": snapshot,
        "snapshot_bytes": 0,
        "snapshot_version": 1 if snapshot else 0,
        "metadata": {},
        "last_used_at": None,
    }


def test_repo_sandbox_git_environment_is_fully_noninteractive(tmp_path) -> None:
    from connectors.repo_sandbox_runner import _env

    env = _env(tmp_path)

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "Never"
    assert env["GIT_ASKPASS"] == "/bin/false"
    assert env["SSH_ASKPASS"] == "/bin/false"


async def _wire_memory_store(monkeypatch, connector, workspace):
    state = dict(workspace)

    async def load_or_create(**kwargs):
        assert kwargs["org_id"] == state["organization_id"]
        assert kwargs["task_id"] == state["task_id"]
        assert kwargs["repo_path"] == state["repo_path"]
        return dict(state)

    async def load(org_id, task_id, repo_path):
        if (org_id, task_id, repo_path) != (
            state["organization_id"],
            state["task_id"],
            state["repo_path"],
        ):
            return None
        return dict(state)

    async def claim(current, lease_id):
        current = dict(current)
        current["lease_owner"] = lease_id
        return current

    async def update(_workspace_id, **values):
        state.update(values)
        return dict(state)

    async def release(_workspace_id, _lease_id):
        state["lease_owner"] = None

    monkeypatch.setattr(connector, "_load_or_create_workspace", load_or_create)
    monkeypatch.setattr(connector, "_load_workspace", load)
    monkeypatch.setattr(connector, "_claim_workspace", claim)
    monkeypatch.setattr(connector, "_update_workspace", update)
    monkeypatch.setattr(connector, "_release_workspace", release)
    return state


@pytest.mark.asyncio
async def test_production_repo_clone_is_persistent_tenant_scoped_and_never_shell_interpolates_user_text(
    monkeypatch,
) -> None:
    import connectors.repo_workspace_remote as remote_module
    from connectors.repo_workspace_remote import ProductionRepoWorkspaceConnector

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "e2b_repo_enabled", True)
    monkeypatch.setattr(settings, "e2b_repo_template_id", "repo-template")
    monkeypatch.setattr(settings, "e2b_repo_allow_internet_access", True)
    connector = ProductionRepoWorkspaceConnector()
    runtime = FakeRepoRuntime()
    connector._runtime = runtime
    state = await _wire_memory_store(monkeypatch, connector, _workspace())
    monkeypatch.setattr(connector, "_download_private_archive", lambda **_kwargs: _async_none())
    stored: dict[str, bytes] = {}

    async def put_object(key, body, _content_type):
        stored[key] = body

    async def no_delete(_key):
        return None

    async def no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(remote_module, "put_object", put_object)
    monkeypatch.setattr(remote_module, "delete_object", no_delete)
    monkeypatch.setattr(remote_module.audit, "log", no_audit)

    source_url = "https://github.com/example/private-looking-repo"
    result = await connector.execute(
        "repo.clone",
        {
            "source_url": source_url,
            "__org_id": "org-a",
            "__task_id": "task-a",
            "__member_id": "member-a",
        },
    )

    assert result.data["execution_boundary"] == "isolated_runtime"
    assert result.data["host_execution"] is False
    assert result.data["workspace"]["persistent"] is True
    assert result.data["workspace"]["resumable"] is True
    assert result.data["files"] == ["README.md", "src/app.py"]
    assert "sandbox_id" not in result.data["workspace"]
    assert "snapshot_object_key" not in result.data["workspace"]
    assert stored and next(iter(stored.values())) == b"durable-repo-snapshot"
    assert state["snapshot_version"] == 1
    # Commands contain only fixed runner/control paths and generated ids. The
    # model-provided URL lives in the JSON control file, never in a shell.
    assert all(source_url not in command for command in runtime.commands)
    assert any(command.startswith("python3 ") for command in runtime.commands)
    assert runtime.created[0]["metadata"] == {
        "org": "org-a",
        "task": "task-a",
        "workspace": "workspace-1",
        "profile": "repo",
    }


async def _async_none():
    return None


@pytest.mark.asyncio
async def test_expired_repo_sandbox_restores_latest_snapshot_on_another_replica(monkeypatch) -> None:
    import connectors.repo_workspace_remote as remote_module
    from connectors.repo_workspace_remote import ProductionRepoWorkspaceConnector

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "e2b_repo_enabled", True)
    monkeypatch.setattr(settings, "e2b_repo_template_id", "repo-template")
    monkeypatch.setattr(settings, "e2b_repo_allow_internet_access", True)
    connector = ProductionRepoWorkspaceConnector()
    runtime = FakeRepoRuntime(expired={"sandbox-old"})
    connector._runtime = runtime
    state = await _wire_memory_store(
        monkeypatch,
        connector,
        _workspace(sandbox_id="sandbox-old", snapshot="repo-workspaces/snapshot.tar.gz"),
    )

    async def get_snapshot(key):
        assert key == "repo-workspaces/snapshot.tar.gz"
        return b"durable-repo-snapshot"

    async def no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(remote_module, "get_object", get_snapshot)
    monkeypatch.setattr(remote_module.audit, "log", no_audit)

    result = await connector.execute(
        "repo.status",
        {
            "repo_path": "repos/imported",
            "__org_id": "org-a",
            "__task_id": "task-a",
            "__member_id": "member-a",
        },
    )

    assert result.data["branch"] == "main"
    assert runtime.keepalives == ["sandbox-old"]
    assert runtime.created and state["sandbox_id"] == "sandbox-1"
    assert "restore" in runtime.actions
    assert result.data["workspace"]["resumable"] is True


def test_isolated_repo_runner_uses_argv_and_enforces_workspace_paths(tmp_path, monkeypatch) -> None:
    import connectors.repo_sandbox_runner as runner

    monkeypatch.setattr(runner, "ROOT", tmp_path.resolve())
    runner.execute(
        {
            "action": "write_uninitialized",
            "repo_path": "repos/demo",
            "path": "test_app.py",
            "content_b64": "ZGVmIHRlc3Rfb2soKToKICAgIGFzc2VydCAxICsgMSA9PSAyCg==",
        }
    )
    initialized = runner.execute(
        {"action": "init", "repo_path": "repos/demo", "message": "Initial snapshot"}
    )
    assert initialized["sha"]
    tests = runner.execute(
        {
            "action": "run_tests",
            "repo_path": "repos/demo",
            "command": [sys.executable, "-m", "pytest", "-q", "test_app.py"],
            "timeout_seconds": 30,
        }
    )
    assert tests["status"] == "success"
    runner.execute(
        {"action": "create_branch", "repo_path": "repos/demo", "branch": "feature/safe"}
    )
    runner.execute(
        {
            "action": "write_file",
            "repo_path": "repos/demo",
            "path": "app.py",
            "content_b64": "cHJpbnQoJ29rJykK",
        }
    )
    runner.execute(
        {
            "action": "commit",
            "repo_path": "repos/demo",
            "message": "fix: publish safe change",
        }
    )
    changes = runner.execute(
        {
            "action": "publication_changes",
            "repo_path": "repos/demo",
            "base_sha": initialized["sha"],
        }
    )
    assert changes["branch"] == "feature/safe"
    assert changes["dirty"] is False
    assert changes["additions"] == [
        {"path": "app.py", "contents": "cHJpbnQoJ29rJykK"}
    ]
    published = runner.execute(
        {
            "action": "create_pr",
            "repo_path": "repos/demo",
            "request_id": "request-7",
            "title": "Fix production bug",
            "body": "Verified test evidence.",
            "base": "main",
            "head": "feature/safe",
            "approval_id": "approval-7",
            "created_at": "2026-07-12T12:00:00+00:00",
            "provider": {
                "id": 7001,
                "node_id": "PR_node_7",
                "number": 7,
                "commit_oid": "c" * 40,
                "url": "https://github.com/acme/widget/pull/7",
            },
        }
    )
    assert published == {
        "status": "published",
        "provider": "github",
        "provider_pr_id": 7001,
        "provider_node_id": "PR_node_7",
        "provider_number": 7,
        "provider_commit_oid": "c" * 40,
        "url": "https://github.com/acme/widget/pull/7",
        "artifact_path": ".chronos/pull_request.json",
    }
    artifact = json.loads(
        (tmp_path / "repos/demo/.chronos/pull_request.json").read_text(encoding="utf-8")
    )
    assert artifact["publication"] == "github_pull_request_created"
    assert artifact["approval_id"] == "approval-7"
    assert artifact["provider_number"] == 7
    with pytest.raises(ValueError, match="path escapes"):
        runner.execute(
            {"action": "read_file", "repo_path": "repos/demo", "path": "../../../etc/passwd"}
        )


@pytest.mark.asyncio
async def test_production_rejects_host_source_path_before_database_or_runtime(monkeypatch, tmp_path) -> None:
    from connectors.repo_workspace_remote import ProductionRepoWorkspaceConnector

    monkeypatch.setattr(settings, "environment", "production")
    connector = ProductionRepoWorkspaceConnector()
    connector._runtime = FakeRepoRuntime()

    async def should_not_load(*_args, **_kwargs):
        raise AssertionError("invalid production source_path reached workspace storage")

    monkeypatch.setattr(connector, "_load_or_create_workspace", should_not_load)
    with pytest.raises(ValueError, match="development-only"):
        await connector.execute(
            "repo.clone",
            {
                "source_path": str(tmp_path),
                "__org_id": "org-a",
                "__task_id": "task-a",
            },
        )


def test_repo_workspace_migration_has_tenant_scope_leases_and_snapshot_pointer() -> None:
    migration = Path(__file__).parents[1] / "migrations" / "versions" / "0058_repo_workspaces.py"
    source = migration.read_text(encoding="utf-8")
    assert 'down_revision = "0057_browser_operator_remote"' in source
    assert '"organization_id", sa.Text(), nullable=False' in source
    assert '"region", sa.Text(), nullable=False' in source
    assert '"task_id", sa.Text(), nullable=False' in source
    assert '"snapshot_object_key", sa.Text()' in source
    assert '"lease_owner", sa.Text()' in source
    assert "uq_repo_workspaces_org_task_path" in source


@pytest.mark.asyncio
async def test_repo_workspace_store_enforces_tenant_task_lookup_and_cross_replica_lease() -> None:
    from connectors.repo_workspace_remote import (
        ProductionRepoWorkspaceConnector,
        RepoWorkspaceBusy,
    )

    connector = ProductionRepoWorkspaceConnector()
    nonce = "lease-" + uuid.uuid4().hex
    workspace = await connector._load_or_create_workspace(
        org_id=f"org-{nonce}",
        task_id=f"task-{nonce}",
        member_id=f"member-{nonce}",
        repo_path="repos/private",
    )
    try:
        assert (
            await connector._load_workspace(
                "another-org", f"task-{nonce}", "repos/private"
            )
            is None
        )
        assert (
            await connector._load_workspace(
                f"org-{nonce}", "another-task", "repos/private"
            )
            is None
        )
        claimed = await connector._claim_workspace(workspace, "replica-a")
        assert claimed["lease_owner"] == "replica-a"
        with pytest.raises(RepoWorkspaceBusy):
            await connector._claim_workspace(workspace, "replica-b")
        await connector._release_workspace(str(workspace["id"]), "replica-a")
        claimed_again = await connector._claim_workspace(workspace, "replica-b")
        assert claimed_again["lease_owner"] == "replica-b"
    finally:
        table = await reflect_table("repo_workspaces")
        async with engine.begin() as conn:
            await conn.execute(delete(table).where(table.c.id == workspace["id"]))


@pytest.mark.asyncio
async def test_approved_pr_publication_binds_member_credential_and_persists_provider_evidence(
    monkeypatch,
) -> None:
    import connectors.repo_workspace_remote as remote_module
    from connectors.repo_workspace_remote import ProductionRepoWorkspaceConnector

    connector = ProductionRepoWorkspaceConnector()
    workspace = {
        **_workspace(sandbox_id="sandbox-pr"),
        "source_url": "https://github.com/acme/widget",
        "metadata": {
            "source_owner": "acme",
            "source_repo": "widget",
            "source_sha": "b" * 40,
            "local_base_sha": "a" * 40,
        },
    }
    state = dict(workspace)
    artifact_specs: list[dict] = []
    audit_rows: list[dict] = []

    async def remote_action(_runtime, _workspace, spec, **_kwargs):
        if spec["action"] == "publication_changes":
            return {
                "branch": "feature/safe",
                "head_sha": "d" * 40,
                "dirty": False,
                "additions": [{"path": "app.py", "contents": "cHJpbnQoJ29rJykK"}],
                "deletions": [],
                "file_count": 1,
                "total_bytes": 12,
            }
        if spec["action"] == "create_pr":
            artifact_specs.append(dict(spec))
            provider = spec["provider"]
            return {
                "status": "published",
                "provider": "github",
                "provider_pr_id": provider["id"],
                "provider_node_id": provider["node_id"],
                "provider_number": provider["number"],
                "provider_commit_oid": provider["commit_oid"],
                "url": provider["url"],
                "artifact_path": ".chronos/pull_request.json",
            }
        raise AssertionError(spec)

    async def update(_workspace_id, **values):
        state.update(values)
        return dict(state)

    async def vault_ref(org_id, member_id):
        assert (org_id, member_id) == ("org-a", "member-a")
        return "vlt_member_github"

    async def github_token(*, org_id, vault_ref):
        assert (org_id, vault_ref) == ("org-a", "vlt_member_github")
        return TOKEN_FOR_TEST

    class FakePublisher:
        def __init__(self, token, *, transport=None):
            assert token == TOKEN_FOR_TEST
            assert transport is None

        def clear_credentials(self):
            return None

        async def publish(self, *, state, persist, **kwargs):
            assert kwargs["source_sha"] == "b" * 40
            assert kwargs["head"] == "feature/safe"
            commit_state = {
                **state,
                "stage": "commit_created",
                "provider_commit_oid": "c" * 40,
            }
            await persist(commit_state)
            final_state = {
                **commit_state,
                "stage": "pr_created",
                "provider_pr_id": 7001,
                "provider_node_id": "PR_node_7",
                "provider_number": 7,
                "provider_url": "https://github.com/acme/widget/pull/7",
            }
            await persist(final_state)
            return (
                {
                    "id": 7001,
                    "node_id": "PR_node_7",
                    "number": 7,
                    "commit_oid": "c" * 40,
                    "url": "https://github.com/acme/widget/pull/7",
                },
                final_state,
                True,
            )

    async def audit_log(event_type, actor_id, action, **kwargs):
        audit_rows.append(
            {"event_type": event_type, "actor_id": actor_id, "action": action, **kwargs}
        )

    monkeypatch.setattr(connector, "_remote_action", remote_action)
    monkeypatch.setattr(connector, "_update_workspace", update)
    monkeypatch.setattr(connector, "_github_vault_ref", vault_ref)
    monkeypatch.setattr(connector, "_github_token", github_token)
    monkeypatch.setattr(remote_module, "GitHubRepoPublisher", FakePublisher)
    monkeypatch.setattr(remote_module.audit, "log", audit_log)

    result, updated = await connector._create_pr(
        object(),
        workspace,
        {
            "title": "Fix production bug",
            "body": "Verified tests.",
            "base": "main",
            "head": "feature/safe",
            "__approved_by_gate": True,
            "__approval_id": "approval-7",
            "__idempotency_key": "idempotency-7",  # gitleaks:allow - deterministic fixture
        },
        org_id="org-a",
        task_id="task-a",
        member_id="member-a",
    )

    assert result["status"] == "published"
    assert result["url"] == "https://github.com/acme/widget/pull/7"
    assert artifact_specs[0]["approval_id"] == "approval-7"
    publications = updated["metadata"]["pr_publications"]
    stored = next(iter(publications.values()))
    assert stored["stage"] == "pr_created"
    assert stored["provider_number"] == 7
    assert TOKEN_FOR_TEST not in json.dumps(updated, default=str)
    assert TOKEN_FOR_TEST not in json.dumps(audit_rows, default=str)
    assert audit_rows[0]["action"] == "repo.pr_published"
    assert audit_rows[0]["payload"]["approval_id"] == "approval-7"
    assert "title" not in audit_rows[0]["payload"]
    assert "body" not in audit_rows[0]["payload"]


TOKEN_FOR_TEST = "member-scoped-token-never-persist"
