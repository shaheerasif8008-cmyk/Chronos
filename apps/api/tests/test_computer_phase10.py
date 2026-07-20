import pytest
from datetime import datetime, timedelta, timezone
from uuid import uuid4


@pytest.fixture(autouse=True)
def _computer_egress_policy(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "e2b_computer_egress_allowlist", "github.com,api.github.com")


def test_manual_desktop_session_requires_bounded_explicit_consent():
    from datetime import datetime, timedelta, timezone

    from pydantic import ValidationError

    from routers.desktop_sessions import CreateDesktopSessionRequest

    with pytest.raises(ValidationError):
        CreateDesktopSessionRequest(purpose="Edit a deck", consent={})

    request = CreateDesktopSessionRequest(
        purpose="Edit a deck",
        consent={
            "purpose": "Edit a deck",
            "allowed_resources": ["Keynote", "Client Q3 deck"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
            "confirmed_by_user": True,
        },
    )
    assert request.consent["confirmed_by_user"] is True


@pytest.mark.asyncio
async def test_desktop_session_rejects_actions_after_consent_expiry():
    from datetime import datetime, timedelta, timezone

    from connectors.desktop import DesktopConnector

    connector = DesktopConnector()
    created = await connector.create_session(
        organization_id="org-expired-desktop",
        member_id="member-1",
        task_id=None,
        purpose="expired desktop",
        consent={
            "allowed_resources": ["Keynote"],
            "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        },
    )
    with pytest.raises(ValueError, match="consent has expired"):
        await connector.execute(
            "desktop.get_state",
            {"session_id": created["id"], "__org_id": "org-expired-desktop"},
        )


def _agent(org_id: str = "org-1"):
    from core.models import AgentContext

    return AgentContext(
        id="task:phase10",
        org_id=org_id,
        member_id="member-1",
        workspace_id="default",
        task_id="phase10",
    )


def _consent(purpose: str = "phase 10 acceptance", *, minutes: int = 30):
    return {
        "purpose": purpose,
        "capabilities": ["terminal", "files", "desktop", "network", "packages"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(),
        "confirmed_by_user": True,
        "allowed_egress_domains": ["github.com"],
    }


def test_computer_tools_are_registered_for_agent_and_inline_use():
    from runtime.tool_registry import ALL_TOOLS, INLINE_CHAT_TOOLS, tool_name

    all_names = {tool_name(tool) for tool in ALL_TOOLS}
    inline_names = {tool_name(tool) for tool in INLINE_CHAT_TOOLS}

    expected = {
        "computer__create_session",
        "computer__exec",
        "computer__list_files",
        "computer__read_file",
        "computer__write_file",
        "computer__install_package",
        "computer__screenshot",
        "computer__input",
        "computer__pause_session",
        "computer__resume_session",
        "computer__cancel_session",
        "computer__export_artifact",
        "local_computer__grant",
        "local_computer__list_files",
        "local_computer__read_file",
        "local_computer__exec",
        "local_computer__open_app",
        "local_computer__revoke",
    }

    assert expected <= all_names
    assert expected <= inline_names


class _FakeRuntime:
    def __init__(self):
        self.fs: dict[str, dict[str, bytes]] = {}
        self._n = 0
        self.expired: set[str] = set()
        self.metadata: dict[str, dict] = {}
        self.paused: set[str] = set()
        self.actions: list[tuple[str, str, dict]] = []
        self.killed: list[str] = []

    async def create(self, *, timeout_seconds, metadata):
        self._n += 1
        sid = f"sbx-{self._n}"
        self.fs[sid] = {}
        self.metadata[sid] = metadata
        return sid

    async def resume(self, sandbox_id, *, timeout_seconds, expected_metadata):
        self._check(sandbox_id)
        assert self.metadata[sandbox_id] == expected_metadata
        self.paused.discard(sandbox_id)
        return "running"

    async def pause(self, sandbox_id):
        self._check(sandbox_id)
        self.paused.add(sandbox_id)

    def _check(self, sandbox_id):
        if sandbox_id in self.expired:
            from connectors.e2b_runtime import SandboxExpired

            raise SandboxExpired(sandbox_id)

    async def run(self, sandbox_id, command, *, cwd, timeout_seconds):
        self._check(sandbox_id)
        files = self.fs.setdefault(sandbox_id, {})

        def result(status="success", stdout="", stderr="", returncode=0):
            return {
                "status": status,
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": False,
                "stderr_truncated": False,
            }

        if command.startswith("mkdir -p"):
            return result()
        if command.startswith("if [ -d"):
            for path in files:
                if path in command:
                    return result(stdout="file")
            return result(stdout="none")
        if command.startswith("printf "):
            return result(stdout=command[len("printf "):])
        if command.startswith("sleep "):
            try:
                duration = int(command.split()[1])
            except ValueError:
                duration = 0
            if timeout_seconds < duration:
                return result(status="timeout", returncode=124)
            return result()
        return result()

    async def write(self, sandbox_id, path, content):
        self._check(sandbox_id)
        self.fs.setdefault(sandbox_id, {})[path] = content

    async def read(self, sandbox_id, path):
        self._check(sandbox_id)
        return self.fs.get(sandbox_id, {}).get(path, b"")

    async def list(self, sandbox_id, path):
        self._check(sandbox_id)
        out = []
        prefix = path.rstrip("/") + "/"
        for full in self.fs.get(sandbox_id, {}):
            if full.startswith(prefix) and "/" not in full[len(prefix):]:
                out.append({"name": full[len(prefix):], "type": "file"})
        return out

    async def remove(self, sandbox_id, path):
        self.fs.get(sandbox_id, {}).pop(path, None)

    async def keepalive(self, sandbox_id, *, timeout_seconds):
        self._check(sandbox_id)

    async def screenshot(self, sandbox_id):
        self._check(sandbox_id)
        return b"\x89PNG\r\n\x1a\n"

    async def desktop_action(self, sandbox_id, action, payload):
        self._check(sandbox_id)
        self.actions.append((sandbox_id, action, payload))

    async def kill(self, sandbox_id):
        self.killed.append(sandbox_id)
        self.fs.pop(sandbox_id, None)


@pytest.mark.asyncio
async def test_cloud_computer_runs_in_isolated_runtime_and_exports_artifact():
    from connectors.computer import computer_connector
    from connectors.e2b_runtime import SANDBOX_ROOT
    from core import tool_broker
    from core.artifacts import read_artifact_content

    computer_connector._runtime = _FakeRuntime()
    try:
        agent = _agent(f"org-cloud-{uuid4()}")
        created = await tool_broker.execute(
            agent,
            "computer.create_session",
            {
                "purpose": "phase 10 acceptance",
                "consent": _consent(),
                "__approved_by_gate": True,
            },
        )
        session_id = created.data["session"]["id"]
        assert "sandbox_id" not in created.data["session"]
        assert "environment" not in created.data["session"]

        wrote = await tool_broker.execute(
            agent,
            "computer.write_file",
            {"session_id": session_id, "path": "app/index.html", "content": "<h1>Chronos</h1>"},
        )
        assert wrote.data["path"] == "app/index.html"

        listed = await tool_broker.execute(agent, "computer.list_files", {"session_id": session_id, "path": "app"})
        assert {entry["path"] for entry in listed.data["entries"]} == {"app/index.html"}

        ran = await tool_broker.execute(
            agent,
            "computer.exec",
            {"session_id": session_id, "command": "printf ready", "timeout_seconds": 2, "__approved_by_gate": True},
        )
        assert ran.data["status"] == "success"
        assert ran.data["stdout"] == "ready"
        assert ran.data["workspace"] == SANDBOX_ROOT

        timed_out = await tool_broker.execute(
            agent,
            "computer.exec",
            {"session_id": session_id, "command": "sleep 2", "timeout_seconds": 1, "__approved_by_gate": True},
        )
        assert timed_out.data["status"] == "timeout"

        screenshot = await tool_broker.execute(agent, "computer.screenshot", {"session_id": session_id})
        assert screenshot.data["status"] == "success"
        assert screenshot.data["screenshot_data_url"].startswith("data:image/png;base64,")
        assert screenshot.data["session"]["id"] == session_id

        clicked = await tool_broker.execute(
            agent,
            "computer.input",
            {
                "session_id": session_id,
                "action": "click",
                "x": 100,
                "y": 120,
                "__approved_by_gate": True,
            },
        )
        assert clicked.data["status"] == "success"
        assert computer_connector._runtime.actions[-1][1] == "click"

        exported = await tool_broker.execute(
            agent,
            "computer.export_artifact",
            {"session_id": session_id, "path": "app/index.html", "title": "Phase 10 App"},
        )
        artifact_id = exported.data["artifact_id"]
        assert await read_artifact_content(artifact_id) == b"<h1>Chronos</h1>"

        events = await computer_connector.list_events(session_id, organization_id=agent.org_id)
        event_types = [event["event_type"] for event in events]
        assert "computer_command" in event_types
        assert "computer_artifact_exported" in event_types
    finally:
        computer_connector._runtime = None


@pytest.mark.asyncio
async def test_cloud_computer_unavailable_without_runtime():
    from connectors.computer import computer_connector
    from core import tool_broker

    computer_connector._runtime = None
    import connectors.computer as comp_mod

    original = comp_mod.computer_runtime
    comp_mod.computer_runtime = lambda **_kwargs: None
    try:
        agent = _agent(f"org-none-{uuid4()}")
        result = await tool_broker.execute(
            agent,
            "computer.create_session",
            {
                "purpose": "unavailable proof",
                "consent": _consent("unavailable proof"),
                "__approved_by_gate": True,
            },
        )
        assert result.data["status"] == "unavailable"
        assert "E2B_API_KEY" in result.data["reason"]
    finally:
        comp_mod.computer_runtime = original


@pytest.mark.asyncio
async def test_cloud_computer_marks_expired_session_truthfully():
    from connectors.computer import computer_connector
    from core import tool_broker

    fake = _FakeRuntime()
    computer_connector._runtime = fake
    try:
        agent = _agent(f"org-expired-{uuid4()}")
        created = await tool_broker.execute(
            agent,
            "computer.create_session",
            {
                "purpose": "expiry proof",
                "consent": _consent("expiry proof"),
                "__approved_by_gate": True,
            },
        )
        session_id = created.data["session"]["id"]
        sandbox_id = computer_connector._sessions[session_id]["environment"]["sandbox_id"]
        fake.expired.add(sandbox_id)

        result = await tool_broker.execute(
            agent,
            "computer.exec",
            {"session_id": session_id, "command": "printf stale", "__approved_by_gate": True},
        )
        assert result.data["status"] == "expired"
        assert result.data["session"]["status"] == "expired"
        assert computer_connector._sessions[session_id]["status"] == "expired"
        assert "Create a new session" in result.data["reason"]
    finally:
        computer_connector._runtime = None


@pytest.mark.asyncio
async def test_cloud_computer_pause_resume_cancel_and_consent_expiry():
    from connectors.computer import computer_connector
    from core import tool_broker

    fake = _FakeRuntime()
    computer_connector._runtime = fake
    try:
        agent = _agent(f"org-lifecycle-{uuid4()}")
        created = await tool_broker.execute(
            agent,
            "computer.create_session",
            {
                "purpose": "lifecycle proof",
                "consent": _consent("lifecycle proof"),
                "__approved_by_gate": True,
            },
        )
        session_id = created.data["session"]["id"]
        sandbox_id = computer_connector._sessions[session_id]["environment"]["sandbox_id"]

        paused = await tool_broker.execute(
            agent, "computer.pause_session", {"session_id": session_id}
        )
        assert paused.data["session"]["status"] == "paused"
        assert sandbox_id in fake.paused

        resumed = await tool_broker.execute(
            agent, "computer.resume_session", {"session_id": session_id}
        )
        assert resumed.data["session"]["status"] == "active"
        assert sandbox_id not in fake.paused

        cancelled = await tool_broker.execute(
            agent,
            "computer.cancel_session",
            {"session_id": session_id, "__approved_by_gate": True},
        )
        assert cancelled.data["session"]["status"] == "cancelled"
        assert sandbox_id in fake.killed

        expiring = await tool_broker.execute(
            agent,
            "computer.create_session",
            {
                "purpose": "hard expiry proof",
                "consent": _consent("hard expiry proof"),
                "__approved_by_gate": True,
            },
        )
        expiring_id = expiring.data["session"]["id"]
        expiring_session = computer_connector._sessions[expiring_id]
        expiring_sandbox = expiring_session["environment"]["sandbox_id"]
        expiring_session["editor_state"]["consent"]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        await computer_connector._save_session(expiring_session)
        with pytest.raises(PermissionError, match="consent has expired"):
            await tool_broker.execute(
                agent, "computer.screenshot", {"session_id": expiring_id}
            )
        expired_session = await computer_connector._load_session(expiring_id, agent.org_id)
        assert expired_session["status"] == "expired"
        assert expiring_sandbox in fake.killed
    finally:
        computer_connector._runtime = None


@pytest.mark.asyncio
async def test_cloud_computer_session_is_tenant_scoped_and_creation_needs_approval():
    from connectors.computer import computer_connector
    from core import tool_broker
    from core.exceptions import ApprovalRequired

    fake = _FakeRuntime()
    computer_connector._runtime = fake
    try:
        owner = _agent(f"org-owner-{uuid4()}")
        with pytest.raises(ApprovalRequired):
            await tool_broker.execute(
                owner,
                "computer.create_session",
                {"purpose": "approval proof", "consent": _consent("approval proof")},
            )
        created = await tool_broker.execute(
            owner,
            "computer.create_session",
            {
                "purpose": "tenant proof",
                "consent": _consent("tenant proof"),
                "__approved_by_gate": True,
            },
        )
        outsider = _agent(f"org-outsider-{uuid4()}")
        with pytest.raises(KeyError):
            await tool_broker.execute(
                outsider,
                "computer.screenshot",
                {"session_id": created.data["session"]["id"]},
            )
    finally:
        computer_connector._runtime = None


@pytest.mark.asyncio
async def test_local_computer_bridge_blocks_unauthorized_folder_and_runs_approved_command(tmp_path):
    from connectors.computer import computer_connector
    from core import tool_broker
    from core.exceptions import ApprovalRequired

    agent = _agent(f"org-local-{uuid4()}")
    authorized = tmp_path / "authorized"
    authorized.mkdir()
    (authorized / "input.txt").write_text("local data", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    granted = await tool_broker.execute(
        agent,
        "local_computer.grant",
        {"folder_path": str(authorized), "purpose": "phase 10 local proof"},
    )
    grant_id = granted.data["grant"]["id"]

    listed = await tool_broker.execute(
        agent,
        "local_computer.list_files",
        {"grant_id": grant_id, "path": "."},
    )
    assert listed.data["entries"][0]["path"] == "input.txt"

    with pytest.raises(PermissionError):
        await tool_broker.execute(
            agent,
            "local_computer.read_file",
            {"grant_id": grant_id, "path": "../outside.txt"},
        )

    with pytest.raises(ApprovalRequired):
        await tool_broker.execute(
            agent,
            "local_computer.exec",
            {"grant_id": grant_id, "command": "printf local"},
        )

    ran = await tool_broker.execute(
        agent,
        "local_computer.exec",
        {"grant_id": grant_id, "command": "printf local", "__approved_by_gate": True},
    )
    assert ran.data["status"] == "success"
    assert ran.data["stdout"] == "local"

    opened = await tool_broker.execute(
        agent,
        "local_computer.open_app",
        {"grant_id": grant_id, "app": "TextEdit", "__approved_by_gate": True},
    )
    assert opened.data["status"] in {"degraded", "success"}

    revoked = await tool_broker.execute(agent, "local_computer.revoke", {"grant_id": grant_id})
    assert revoked.data["grant"]["status"] == "revoked"

    with pytest.raises(PermissionError):
        await tool_broker.execute(
            agent,
            "local_computer.list_files",
            {"grant_id": grant_id, "path": "."},
        )

    events = await computer_connector.list_local_events(grant_id, organization_id=agent.org_id)
    assert "local_command" in [event["event_type"] for event in events]
