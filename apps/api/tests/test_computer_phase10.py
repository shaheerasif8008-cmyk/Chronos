import pytest
from uuid import uuid4


def _agent(org_id: str = "org-1"):
    from core.models import AgentContext

    return AgentContext(
        id="task:phase10",
        org_id=org_id,
        member_id="member-1",
        workspace_id="default",
        task_id="phase10",
    )


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


@pytest.mark.asyncio
async def test_cloud_computer_runs_in_sandbox_enforces_timeout_and_exports_artifact():
    from connectors.computer import computer_connector
    from core import tool_broker
    from core.artifacts import read_artifact_content

    agent = _agent(f"org-cloud-{uuid4()}")
    created = await tool_broker.execute(
        agent,
        "computer.create_session",
        {"purpose": "phase 10 acceptance"},
    )
    session_id = created.data["session"]["id"]

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
        {"session_id": session_id, "command": "printf ready", "timeout_seconds": 2},
    )
    assert ran.data["status"] == "success"
    assert ran.data["stdout"] == "ready"
    assert f"/chronos_computers/{agent.org_id}/" in ran.data["workspace"]

    timed_out = await tool_broker.execute(
        agent,
        "computer.exec",
        {"session_id": session_id, "command": "sleep 2", "timeout_seconds": 1},
    )
    assert timed_out.data["status"] == "timeout"

    screenshot = await tool_broker.execute(agent, "computer.screenshot", {"session_id": session_id})
    assert screenshot.data["status"] in {"degraded", "success"}
    assert screenshot.data["session"]["id"] == session_id

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
