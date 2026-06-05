from __future__ import annotations

import pytest


def test_repo_workspace_tools_registered_for_agent_runtime():
    from runtime.tool_registry import ALL_TOOLS, SUBAGENT_TOOLS, to_broker_name, tool_name

    expected = {
        "repo__clone",
        "repo__open_fixture",
        "repo__create_branch",
        "repo__list_files",
        "repo__read_file",
        "repo__write_file",
        "repo__run_tests",
        "repo__diff",
        "repo__status",
        "repo__commit",
        "repo__create_pr",
        "repo__review",
    }
    names = {tool_name(schema) for schema in ALL_TOOLS}
    assert expected <= names
    assert expected <= {tool_name(schema) for schema in SUBAGENT_TOOLS}
    assert to_broker_name("repo__run_tests") == "repo.run_tests"
    assert to_broker_name("repo__create_pr") == "repo.create_pr"


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


@pytest.mark.asyncio
async def test_repo_workspace_import_list_status_commit_pr_and_review(tmp_path, monkeypatch):
    from connectors.repo_workspace import repo_workspace_connector

    monkeypatch.setattr("core.workspace.WORKSPACE_ROOT", tmp_path)
    source = tmp_path / "source_repo"
    source.mkdir()
    (source / "README.md").write_text("# demo\n\nTODO: document the fix\n", encoding="utf-8")
    (source / "pkg").mkdir()
    (source / "pkg" / "math.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (source / "test_math.py").write_text(
        "from pkg.math import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    scope = {"__org_id": "default", "__task_id": "task-2"}
    cloned = await repo_workspace_connector.execute(
        "repo.clone",
        {"source_path": str(source), "repo_path": "repos/imported", **scope},
    )
    assert cloned.data["repo_path"] == "repos/imported"
    assert cloned.data["source"]["type"] == "local_path"
    assert cloned.data["branch"] == "main"

    files = await repo_workspace_connector.execute("repo.list_files", {"repo_path": "repos/imported", **scope})
    assert files.data["files"] == ["README.md", "pkg/math.py", "test_math.py"]

    red = await repo_workspace_connector.execute(
        "repo.run_tests",
        {"repo_path": "repos/imported", "command": "pytest -q test_math.py", **scope},
    )
    assert red.data["status"] == "failure"
    assert red.data["command"] == ["pytest", "-q", "test_math.py"]
    assert "1 failed" in (red.data["stdout"] + red.data["stderr"]) or "FAILED" in (red.data["stdout"] + red.data["stderr"])

    await repo_workspace_connector.execute(
        "repo.write_file",
        {"repo_path": "repos/imported", "path": "pkg/math.py", "content": "def add(a, b):\n    return a + b\n", **scope},
    )
    green = await repo_workspace_connector.execute(
        "repo.run_tests",
        {"repo_path": "repos/imported", "command": "pytest -q test_math.py", **scope},
    )
    assert green.data["status"] == "success"

    status = await repo_workspace_connector.execute("repo.status", {"repo_path": "repos/imported", **scope})
    assert status.data["branch"] == "main"
    assert status.data["dirty"] is True
    assert any(item["path"] == "pkg/math.py" for item in status.data["changes"])

    review = await repo_workspace_connector.execute(
        "repo.review",
        {"repo_path": "repos/imported", "title": "Fix arithmetic bug", **scope},
    )
    assert review.data["artifact_path"] == ".chronos/code_review.json"
    assert any(finding["file"] == "README.md" and finding["severity"] == "medium" for finding in review.data["findings"])
    assert review.data["summary"]["changed_files"] == 1

    commit = await repo_workspace_connector.execute(
        "repo.commit",
        {"repo_path": "repos/imported", "message": "fix: correct add implementation", **scope},
    )
    assert commit.data["sha"]
    assert commit.data["message"] == "fix: correct add implementation"

    pr_blocked = await repo_workspace_connector.execute(
        "repo.create_pr",
        {
            "repo_path": "repos/imported",
            "title": "Fix arithmetic bug",
            "body": "Green test run included.",
            "base": "main",
            "head": "main",
            **scope,
        },
    )
    assert pr_blocked.data["status"] == "approval_required"
    assert pr_blocked.data["risk_level"] == "repo_pull_request"

    pr_created = await repo_workspace_connector.execute(
        "repo.create_pr",
        {
            "repo_path": "repos/imported",
            "title": "Fix arithmetic bug",
            "body": "Green test run included.",
            "base": "main",
            "head": "main",
            "approval_id": "approval-1",
            **scope,
        },
    )
    assert pr_created.data["status"] == "ready"
    assert pr_created.data["url"].startswith("chronos://repo-pr/")
    assert pr_created.data["artifact_path"] == ".chronos/pull_request.json"


@pytest.mark.asyncio
async def test_repo_workspace_rejects_shell_test_commands_and_path_escape(tmp_path, monkeypatch):
    from connectors.repo_workspace import repo_workspace_connector

    monkeypatch.setattr("core.workspace.WORKSPACE_ROOT", tmp_path)
    scope = {"__org_id": "default", "__task_id": "task-3"}
    await repo_workspace_connector.execute("repo.open_fixture", {"name": "python_bug", **scope})

    with pytest.raises(ValueError, match="Only pytest commands"):
        await repo_workspace_connector.execute("repo.run_tests", {"command": "pytest -q; cat /etc/passwd", **scope})

    with pytest.raises(ValueError, match="Path escapes"):
        await repo_workspace_connector.execute("repo.clone", {"source_path": str(tmp_path / ".."), **scope})
