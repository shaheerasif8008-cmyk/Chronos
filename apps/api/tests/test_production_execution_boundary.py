"""Focused proof for the production API-host execution boundary."""
from __future__ import annotations

import pytest
from pathlib import Path
from types import SimpleNamespace

from core.config import settings
from core.models import ToolResult


def test_production_boundary_classifies_all_host_backed_tool_families(monkeypatch) -> None:
    from core.execution_boundary import blocks_api_host_tool

    monkeypatch.setattr(settings, "environment", "production")

    for tool in ("desktop.create_session", "desktop.open_app"):
        assert blocks_api_host_tool(tool) is True

    # Repo operations are routed to a persistent E2B sandbox in production.
    for tool in (
        "repo.clone",
        "repo.open_fixture",
        "repo.create_branch",
        "repo.list_files",
        "repo.read_file",
        "repo.write_file",
        "repo.run_tests",
        "repo.diff",
        "repo.status",
        "repo.commit",
        "repo.create_pr",
        "repo.review",
    ):
        assert blocks_api_host_tool(tool) is False

    # Production local-computer actions are no longer API-host-backed: they are
    # routed to an authenticated, separately paired desktop device.
    assert blocks_api_host_tool("local_computer.grant") is False
    assert blocks_api_host_tool("local_computer.exec") is False

    # These paths are backed by E2B and must remain reachable. Their connectors
    # fail closed when the isolated runtime is not configured.
    assert blocks_api_host_tool("code.python") is False
    assert blocks_api_host_tool("data.run") is False
    assert blocks_api_host_tool("computer.exec") is False
    assert blocks_api_host_tool("skill.run_script") is False


@pytest.mark.parametrize(
    "validator_path,validator_name",
    [
        ("connectors.code", "_validate_code"),
        ("connectors.data_analysis", "_validate_data_code"),
    ],
)
def test_lexical_validators_are_not_a_boundary_for_proc_environ(
    validator_path: str, validator_name: str
) -> None:
    """Document the concrete bypass without reading or printing any secret.

    Building the path in a variable bypasses the literal absolute-path regex;
    ``os.open`` can then address the API parent's environment in a shared PID
    namespace.  The production gate, not this denylist, is the security proof.
    """

    module = __import__(validator_path, fromlist=[validator_name])
    validator = getattr(module, validator_name)
    payload = """import os
target = "/proc/" + str(os.getppid()) + "/environ"
fd = os.open(target, os.O_RDONLY)
print(os.read(fd, 65535))
"""

    # This must remain accepted by the test: attempting to grow the lexical
    # denylist into a sandbox would hide the real isolation requirement.
    validator(payload)


@pytest.mark.asyncio
async def test_code_python_fails_closed_when_e2b_is_unconfigured(monkeypatch) -> None:
    from connectors.code import code_connector

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "e2b_api_key", "")
    code_connector._runtime = None

    async def should_not_run(_args):
        raise AssertionError("production reached API-host Python execution")

    monkeypatch.setattr(code_connector, "_python", should_not_run)
    result = await code_connector.execute("code.python", {"code": "print('unsafe')"})

    assert result.data["status"] == "unavailable"
    assert result.data["execution_boundary"] == "isolated_runtime_required"
    assert result.data["host_execution"] is False


@pytest.mark.asyncio
async def test_data_run_fails_closed_when_e2b_is_unconfigured_before_materialization(monkeypatch) -> None:
    from connectors.data_analysis import data_analysis_connector

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "e2b_api_key", "")
    data_analysis_connector._runtime = None

    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("production reached API-host data execution")

    monkeypatch.setattr(data_analysis_connector, "_run", should_not_run)
    result = await data_analysis_connector.execute(
        "data.run", {"dataset_id": "dataset", "code": "print('unsafe')"}
    )

    assert result.data["status"] == "unavailable"
    assert result.data["host_execution"] is False


class _FakeExecutionRuntime:
    def __init__(self, *, stdout: str = "", chart: bytes | None = None) -> None:
        self.stdout = stdout
        self.chart = chart
        self.created: list[dict] = []
        self.writes: dict[str, bytes] = {}
        self.killed: list[str] = []

    async def create(self, *, timeout_seconds, metadata):
        self.created.append({"timeout_seconds": timeout_seconds, "metadata": metadata})
        return "sandbox-production"

    async def run(self, sandbox_id, command, *, cwd, timeout_seconds):
        assert sandbox_id == "sandbox-production"
        assert command in {"python3 code.py", "python3 analysis.py"}
        return {
            "status": "success",
            "returncode": 0,
            "stdout": self.stdout,
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }

    async def write(self, sandbox_id, path, content):
        assert sandbox_id == "sandbox-production"
        self.writes[path] = bytes(content)

    async def read(self, sandbox_id, path):
        assert sandbox_id == "sandbox-production"
        if path.endswith("chart.png") and self.chart is not None:
            return self.chart
        return b""

    async def list(self, sandbox_id, path):
        assert sandbox_id == "sandbox-production"
        if self.chart is None:
            return []
        return [{"name": "chart.png", "type": "file"}]

    async def kill(self, sandbox_id):
        self.killed.append(sandbox_id)


@pytest.mark.asyncio
async def test_code_python_uses_ephemeral_e2b_runtime_in_production(monkeypatch) -> None:
    from connectors.code import code_connector
    from connectors.e2b_runtime import SANDBOX_ROOT

    monkeypatch.setattr(settings, "environment", "production")
    runtime = _FakeExecutionRuntime(stdout="isolated\n")
    code_connector._runtime = runtime

    async def should_not_run_on_host(_args):
        raise AssertionError("production reached API-host Python execution")

    monkeypatch.setattr(code_connector, "_python", should_not_run_on_host)
    try:
        result = await code_connector.execute(
            "code.python",
            {
                "code": "print('isolated')",
                "__org_id": "org-safe",
                "__task_id": "task-safe",
            },
        )
    finally:
        code_connector._runtime = None

    assert runtime.writes[f"{SANDBOX_ROOT}/code.py"] == b"print('isolated')"
    assert runtime.created[0]["metadata"] == {
        "org": "org-safe",
        "task": "task-safe",
        "tool": "code.python",
    }
    assert runtime.killed == ["sandbox-production"]
    assert result.data["status"] == "success"
    assert result.data["stdout"] == "isolated\n"
    assert result.data["execution_boundary"] == "isolated_runtime"
    assert result.data["host_execution"] is False


@pytest.mark.asyncio
async def test_data_run_uploads_tenant_checked_data_and_collects_outputs_from_e2b(
    monkeypatch, tmp_path
) -> None:
    import connectors.data_analysis as data_module
    from connectors.e2b_runtime import SANDBOX_ROOT

    monkeypatch.setattr(settings, "environment", "production")
    runtime = _FakeExecutionRuntime(stdout="rows=2\n", chart=b"fake-png")
    data_module.data_analysis_connector._runtime = runtime

    async def fake_materialize(dataset_id: str, org_id: str, workspace: Path):
        assert dataset_id == "dataset-safe"
        assert org_id == "org-safe"
        path = workspace / "data.csv"
        path.write_bytes(b"value\n1\n2\n")
        return path, "data.csv"

    saved: list[tuple[bytes | str, str]] = []

    async def fake_save(content, *, kind, **_kwargs):
        saved.append((content, kind))
        return f"artifact-{len(saved)}"

    monkeypatch.setattr(data_module, "_materialize_dataset", fake_materialize)
    monkeypatch.setattr("core.artifacts.save_artifact", fake_save)

    async def should_not_run_on_host(*_args, **_kwargs):
        raise AssertionError("production reached API-host data subprocess")

    monkeypatch.setattr(
        data_module.data_analysis_connector,
        "_run_in_workspace",
        should_not_run_on_host,
    )
    try:
        result = await data_module.data_analysis_connector.execute(
            "data.run",
            {
                "dataset_id": "dataset-safe",
                "code": "import pandas as pd\nprint(len(pd.read_csv('data.csv')))",
                "__org_id": "org-safe",
                "__task_id": "task-safe",
            },
        )
    finally:
        data_module.data_analysis_connector._runtime = None

    assert runtime.writes[f"{SANDBOX_ROOT}/data.csv"] == b"value\n1\n2\n"
    assert f"{SANDBOX_ROOT}/analysis.py" in runtime.writes
    assert runtime.killed == ["sandbox-production"]
    assert saved == [(b"fake-png", "image"), ("rows=2", "report")]
    assert result.data["artifact_ids"] == ["artifact-1", "artifact-2"]
    assert result.data["execution_boundary"] == "isolated_runtime"
    assert result.data["host_execution"] is False


@pytest.mark.asyncio
async def test_e2b_runtime_applies_template_and_disables_internet(monkeypatch) -> None:
    from connectors.e2b_runtime import E2BRuntime

    captured: dict = {}

    class FakeCommands:
        async def run(self, command):
            if command.startswith("mkdir -p "):
                return SimpleNamespace(exit_code=0)
            assert "1.1.1.1" in command
            return SimpleNamespace(exit_code=0)

    class FakeSandbox:
        sandbox_id = "sandbox-config"
        commands = FakeCommands()

    class FakeAsyncSandbox:
        @classmethod
        async def create(cls, **kwargs):
            captured.update(kwargs)
            return FakeSandbox()

    monkeypatch.setitem(__import__("sys").modules, "e2b", SimpleNamespace(AsyncSandbox=FakeAsyncSandbox))
    runtime = E2BRuntime(
        "secret-key",
        template="chronos-data-v1",
        allow_internet_access=False,
    )
    sandbox_id = await runtime.create(timeout_seconds=90, metadata={"org": "org-safe"})

    assert sandbox_id == "sandbox-config"
    assert captured["template"] == "chronos-data-v1"
    assert captured["allow_internet_access"] is False
    assert captured["network"] == {
        "deny_out": ["0.0.0.0/0", "::/0"],
        "allow_public_traffic": False,
    }
    assert captured["metadata"]["chronos_egress_policy"] == "deny_all_v1"
    assert captured["api_key"] == "secret-key"


@pytest.mark.asyncio
async def test_e2b_runtime_destroys_sandbox_when_deny_egress_probe_connects(monkeypatch) -> None:
    from connectors.e2b_runtime import E2BRuntime, RuntimeUnavailable

    class FakeCommands:
        async def run(self, command):
            return SimpleNamespace(exit_code=42 if "1.1.1.1" in command else 0)

    class FakeSandbox:
        sandbox_id = "sandbox-leaky"
        commands = FakeCommands()

        def __init__(self):
            self.killed = False

        async def kill(self):
            self.killed = True

    sandbox = FakeSandbox()

    class FakeAsyncSandbox:
        @classmethod
        async def create(cls, **_kwargs):
            return sandbox

    monkeypatch.setitem(__import__("sys").modules, "e2b", SimpleNamespace(AsyncSandbox=FakeAsyncSandbox))
    runtime = E2BRuntime("secret-key", allow_internet_access=False)

    with pytest.raises(RuntimeUnavailable, match="startup check"):
        await runtime.create(timeout_seconds=90, metadata={"org": "org-safe"})

    assert sandbox.killed is True
    assert "sandbox-leaky" not in runtime._connected


@pytest.mark.asyncio
async def test_e2b_network_profile_is_domain_allowlisted_and_attested(monkeypatch) -> None:
    from connectors.e2b_runtime import E2BRuntime

    captured: dict = {}
    commands: list[str] = []

    class FakeCommands:
        async def run(self, command, **_kwargs):
            commands.append(command)
            return SimpleNamespace(exit_code=0)

    class FakeSandbox:
        sandbox_id = "sandbox-allowlisted"
        commands = FakeCommands()

    class FakeAsyncSandbox:
        @classmethod
        async def create(cls, **kwargs):
            captured.update(kwargs)
            return FakeSandbox()

    monkeypatch.setitem(__import__("sys").modules, "e2b", SimpleNamespace(AsyncSandbox=FakeAsyncSandbox))
    runtime = E2BRuntime(
        "secret-key",
        allow_internet_access=True,
        egress_allowlist=["github.com", "api.github.com"],
    )

    assert await runtime.create(timeout_seconds=90, metadata={}) == "sandbox-allowlisted"
    assert captured["network"] == {
        "allow_out": ["github.com", "api.github.com"],
        "deny_out": ["0.0.0.0/0", "::/0"],
        "allow_public_traffic": False,
    }
    assert captured["metadata"]["chronos_egress_policy"].startswith("allowlist_v1:")
    assert any("github.com" in command and "1.1.1.1" in command for command in commands)


def test_e2b_network_profile_refuses_ambient_internet_without_allowlist() -> None:
    from connectors.e2b_runtime import E2BRuntime

    with pytest.raises(ValueError, match="egress allowlist"):
        E2BRuntime("secret-key", allow_internet_access=True)


def test_default_e2b_runtime_is_offline_even_when_computer_network_is_enabled(monkeypatch) -> None:
    from connectors.e2b_runtime import E2BRuntime, default_runtime

    monkeypatch.setattr(settings, "e2b_api_key", "configured-key")
    monkeypatch.setattr(settings, "e2b_template_id", "chronos-runtime-v1")
    monkeypatch.setattr(settings, "e2b_allow_internet_access", True)
    monkeypatch.setattr(settings, "e2b_computer_allow_internet_access", True)
    monkeypatch.setattr(settings, "e2b_computer_egress_allowlist", "github.com")

    runtime = default_runtime()

    assert isinstance(runtime, E2BRuntime)
    assert runtime._template == "chronos-runtime-v1"
    assert runtime._allow_internet_access is False


def test_computer_e2b_runtime_uses_its_separate_network_profile(monkeypatch) -> None:
    from connectors.e2b_runtime import E2BRuntime, computer_runtime

    monkeypatch.setattr(settings, "e2b_api_key", "configured-key")
    monkeypatch.setattr(settings, "e2b_template_id", "chronos-runtime-v1")
    monkeypatch.setattr(settings, "e2b_computer_allow_internet_access", True)
    monkeypatch.setattr(settings, "e2b_computer_egress_allowlist", "github.com")

    runtime = computer_runtime()

    assert isinstance(runtime, E2BRuntime)
    assert runtime._allow_internet_access is True
    assert runtime._egress_allowlist == ["github.com"]


def test_computer_network_can_be_explicitly_disabled_without_changing_offline_profile(
    monkeypatch,
) -> None:
    from connectors.e2b_runtime import computer_runtime, default_runtime

    monkeypatch.setattr(settings, "e2b_api_key", "configured-key")
    monkeypatch.setattr(settings, "e2b_computer_allow_internet_access", False)

    assert computer_runtime()._allow_internet_access is False
    assert default_runtime()._allow_internet_access is False


@pytest.mark.asyncio
async def test_connector_health_reports_production_code_and_data_as_isolated(monkeypatch) -> None:
    from core import connector_health

    async def no_playwright():
        return False, "Playwright is not installed."

    async def verified_e2b():
        return connector_health.ProbeResult(
            ok=True,
            checked_at=connector_health._utcnow(),
            latency_ms=9,
        )

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "e2b_api_key", "configured-key")
    monkeypatch.setattr(settings, "e2b_allow_internet_access", False)
    monkeypatch.setattr(settings, "e2b_repo_enabled", True)
    monkeypatch.setattr(settings, "e2b_repo_template_id", "repo-template")
    monkeypatch.setattr(settings, "e2b_repo_allow_internet_access", True)
    monkeypatch.setattr(settings, "e2b_repo_egress_allowlist", "github.com,pypi.org")
    monkeypatch.setattr(settings, "e2b_computer_egress_allowlist", "github.com")
    monkeypatch.setattr(connector_health, "_playwright_available", no_playwright)
    monkeypatch.setattr(connector_health, "_probe_e2b", verified_e2b)
    monkeypatch.setattr(connector_health, "_CACHE", None)

    health = await connector_health.check_connectors(refresh=True)

    assert health["code"]["status"] == "verified"
    assert health["code"]["tier"] == "live"
    assert health["data"]["status"] == "verified"
    assert health["data"]["verified"] is True
    assert health["repo"]["status"] == "verified"
    assert health["repo"]["tier"] == "live"
    assert "ephemeral E2B" in health["code"]["reason"]
    assert "internet access disabled" in health["data"]["reason"]
    assert "Postgres leases" in health["repo"]["reason"]
    assert health["repo"]["setup"] is None


@pytest.mark.asyncio
async def test_production_repo_tools_fail_closed_without_tenant_task_scope_before_host_access(monkeypatch) -> None:
    import connectors.repo_workspace as repo_module

    monkeypatch.setattr(settings, "environment", "production")

    def should_not_touch_host(*_args, **_kwargs):
        raise AssertionError("production repo tool touched the API-host workspace")

    async def should_not_spawn(*_args, **_kwargs):
        raise AssertionError("production repo tool spawned an API-host process")

    monkeypatch.setattr(repo_module, "task_workspace_root_from_args", should_not_touch_host)
    monkeypatch.setattr(repo_module, "_run", should_not_spawn)
    monkeypatch.setattr(repo_module.shutil, "copytree", should_not_touch_host)

    for tool in (
        "repo.clone",
        "repo.open_fixture",
        "repo.create_branch",
        "repo.list_files",
        "repo.read_file",
        "repo.write_file",
        "repo.run_tests",
        "repo.diff",
        "repo.status",
        "repo.commit",
        "repo.create_pr",
        "repo.review",
    ):
        result = await repo_module.repo_workspace_connector.execute(tool, {})
        assert result.data["status"] == "unavailable"
        assert result.data["execution_boundary"] == "isolated_runtime_required"
        assert result.data["host_execution"] is False
        assert result.data["error_code"] == "tenant_task_scope_required"


@pytest.mark.asyncio
async def test_tool_broker_route_dispatches_repo_with_explicit_scope(monkeypatch) -> None:
    import connectors.repo_workspace as repo_module
    from core.models import AgentContext
    from core.tool_broker import _route

    monkeypatch.setattr(settings, "environment", "production")

    async def isolated_dispatch(tool, args):
        assert tool == "repo.run_tests"
        assert args["__org_id"] == "org-safe"
        assert args["__task_id"] == "task-safe"
        return ToolResult(
            data={"status": "success", "execution_boundary": "isolated_runtime", "host_execution": False},
            summary="isolated",
        )

    monkeypatch.setattr(repo_module.repo_workspace_connector, "execute", isolated_dispatch)
    result = await _route(
        AgentContext(
            id="agent-safe",
            org_id="org-safe",
            member_id="member-safe",
            task_id="task-safe",
        ),
        "repo.run_tests",
        {},
        "unavailable",
        "unavailable",
    )

    assert result.data["status"] == "success"
    assert result.data["host_execution"] is False


@pytest.mark.asyncio
async def test_production_tool_resolution_exposes_isolated_repo_family(monkeypatch) -> None:
    import core.connector_tools as connector_tools
    from runtime.tool_registry import BROWSER_SEARCH, REPO_WORKSPACE_TOOLS

    monkeypatch.setattr(settings, "environment", "production")

    async def no_connections(_org_id, _member_id=None):
        return {}

    async def no_permissions(_org_id):
        return {}

    monkeypatch.setattr(connector_tools, "connected_providers", no_connections)
    monkeypatch.setattr("core.settings_store.tool_permissions", no_permissions)

    resolved = await connector_tools.resolve_agent_tools(
        [BROWSER_SEARCH, *REPO_WORKSPACE_TOOLS],
        org_id="org-safe",
        member_id="member-safe",
    )
    names = {item["function"]["name"] for item in resolved}

    assert "browser__search" in names
    assert {item["function"]["name"] for item in REPO_WORKSPACE_TOOLS} <= names


@pytest.mark.asyncio
async def test_local_computer_fails_closed_in_production_before_host_access(monkeypatch) -> None:
    from connectors.computer import ComputerConnector
    from core import desktop_bridge as bridge_module
    from core.desktop_bridge import DesktopBridgeService, MemoryDesktopBridgeStore

    monkeypatch.setattr(settings, "environment", "production")
    connector = ComputerConnector()

    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("production reached API-host local computer bridge")

    monkeypatch.setattr(connector, "_execute_local", should_not_run)
    bridge = DesktopBridgeService(MemoryDesktopBridgeStore())
    monkeypatch.setattr(bridge_module, "desktop_bridge", bridge)
    result = await connector.execute("local_computer.grant", {"folder_path": "/"})

    assert result.data["status"] == "requires_device_grant"
    assert result.data["host_execution"] is False
    assert result.data["execution_boundary"] == "authenticated_desktop_device"


@pytest.mark.asyncio
async def test_desktop_fails_closed_in_production_before_xvfb_or_shell(monkeypatch) -> None:
    from connectors.desktop import DesktopConnector

    monkeypatch.setattr(settings, "environment", "production")
    connector = DesktopConnector()

    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("production reached API-host desktop execution")

    monkeypatch.setattr(connector, "create_session", should_not_run)
    result = await connector.execute("desktop.create_session", {})

    assert result.data["status"] == "unavailable"
    assert result.data["host_execution"] is False


@pytest.mark.asyncio
async def test_cloud_computer_keeps_the_genuine_isolated_path_in_production(monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta, timezone

    from connectors.computer import ComputerConnector

    monkeypatch.setattr(settings, "environment", "production")

    class FakeIsolatedRuntime:
        created = False

        async def create(self, *, timeout_seconds, metadata):
            self.created = True
            assert metadata["chronos_session"]
            assert len(metadata["chronos_tenant"]) == 64
            return "sandbox-safe"

        async def run(self, *_args, **_kwargs):
            return {"status": "success", "returncode": 0, "stdout": "", "stderr": ""}

        async def write(self, *_args, **_kwargs):
            return None

        async def read(self, *_args, **_kwargs):
            return b""

        async def list(self, *_args, **_kwargs):
            return []

        async def kill(self, *_args, **_kwargs):
            return None

    runtime = FakeIsolatedRuntime()
    connector = ComputerConnector()
    connector._runtime = runtime

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(connector, "_save_session", noop)
    monkeypatch.setattr(connector, "_record_event", noop)

    @asynccontextmanager
    async def quota(_organization_id):
        yield

    monkeypatch.setattr(connector, "_quota_admission", quota)

    result = await connector.execute(
        "computer.create_session",
        {
            "__org_id": "org-safe",
            "__task_id": "task-safe",
            "__member_id": "member-safe",
            "purpose": "proof",
            "consent": {
                "purpose": "proof",
                "capabilities": ["terminal", "files"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "confirmed_by_user": True,
            },
        },
    )

    assert runtime.created is True
    assert result.data["session"]["status"] == "active"


@pytest.mark.asyncio
async def test_development_code_path_remains_explicitly_available(monkeypatch) -> None:
    from connectors.code import code_connector

    monkeypatch.setattr(settings, "environment", "development")

    async def fake_python(_args):
        return ToolResult(data={"status": "success"}, summary="development subprocess")

    monkeypatch.setattr(code_connector, "_python", fake_python)
    result = await code_connector.execute("code.python", {"code": "print('dev')"})

    assert result.data["status"] == "success"
