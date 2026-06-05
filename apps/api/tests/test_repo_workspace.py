from __future__ import annotations

import pytest


def test_repo_workspace_tools_registered_for_agent_runtime():
    from runtime.tool_registry import ALL_TOOLS, SUBAGENT_TOOLS, to_broker_name, tool_name

    expected = {
        "repo__open_fixture",
        "repo__create_branch",
        "repo__read_file",
        "repo__write_file",
        "repo__run_tests",
        "repo__diff",
    }
    names = {tool_name(schema) for schema in ALL_TOOLS}
    assert expected <= names
    assert expected <= {tool_name(schema) for schema in SUBAGENT_TOOLS}
    assert to_broker_name("repo__run_tests") == "repo.run_tests"


@pytest.mark.asyncio
async def test_repo_workspace_fixture_branch_test_patch_and_diff(tmp_path, monkeypatch):
    from connectors.repo_workspace import repo_workspace_connector

    monkeypatch.setattr("core.workspace.WORKSPACE_ROOT", tmp_path)
    scope = {"__org_id": "default", "__task_id": "task-1"}

    opened = await repo_workspace_connector.execute("repo.open_fixture", {"name": "python_bug", **scope})
    assert opened.data["repo_path"] == "repos/python_bug"
    assert "calculator.py" in opened.data["files"]

    branched = await repo_workspace_connector.execute("repo.create_branch", {"branch": "fix/add", **scope})
    assert branched.data["branch"] == "fix/add"

    read = await repo_workspace_connector.execute("repo.read_file", {"path": "calculator.py", **scope})
    assert "return a - b" in read.data["content"]

    red = await repo_workspace_connector.execute("repo.run_tests", {**scope})
    assert red.data["status"] == "failure"
    assert red.data["returncode"] != 0
    red_output = red.data["stdout"] + red.data["stderr"]
    assert "1 failed" in red_output or "FAILED" in red_output

    await repo_workspace_connector.execute(
        "repo.write_file",
        {"path": "calculator.py", "content": "def add(a, b):\n    return a + b\n", **scope},
    )

    green = await repo_workspace_connector.execute("repo.run_tests", {**scope})
    assert green.data["status"] == "success"
    assert green.data["returncode"] == 0

    diff = await repo_workspace_connector.execute("repo.diff", {**scope})
    assert "return a - b" in diff.data["diff"]
    assert "return a + b" in diff.data["diff"]
