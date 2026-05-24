"""Artifact version history + conversation-scoped agent tools.

These exercise the iteration loop: an artifact written under a stable key in a
conversation becomes a new *version* on the next write (rather than a brand-new
artifact), and the agent can list/read/update artifacts across tasks in the same
conversation via the artifact__* tools (fs workspaces are per-task, so fs tools
alone cannot bridge tasks).

DB and object storage are mocked at the function boundary (the established
pattern in this suite), so no live Postgres/MinIO is required.
"""
import json

import pytest

from core.models import ToolResult


# ── save_artifact versioning ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_artifact_creates_v1_when_key_is_new(monkeypatch):
    from core import artifacts

    captured: dict = {}

    async def fake_store(artifact_id, raw, org_id, mime_type):
        return f"minio://{org_id}/{artifact_id}"

    async def fake_current(org_id, scope_id, key):
        return None  # nothing under this key yet

    async def fake_insert(values, supersede_id):
        captured["values"] = values
        captured["supersede_id"] = supersede_id
        return values["id"]

    monkeypatch.setattr(artifacts, "_store_artifact_bytes", fake_store)
    monkeypatch.setattr(artifacts, "_current_artifact_row", fake_current)
    monkeypatch.setattr(artifacts, "_insert_artifact_version", fake_insert)

    new_id = await artifacts.save_artifact(
        "<h1>v1</h1>", kind="html", key="game.html",
        conversation_id="conv-1", org_id="default",
    )

    assert captured["values"]["version"] == 1
    assert captured["values"]["artifact_key"] == "game.html"
    assert captured["values"]["is_current"] is True
    assert captured["supersede_id"] is None
    assert new_id == captured["values"]["id"]


@pytest.mark.asyncio
async def test_save_artifact_bumps_version_and_supersedes_when_key_exists(monkeypatch):
    from core import artifacts

    captured: dict = {}

    async def fake_store(artifact_id, raw, org_id, mime_type):
        return "minio://x"

    async def fake_current(org_id, scope_id, key):
        assert scope_id == "conv-1"  # conversation is the versioning scope
        assert key == "game.html"
        return {"id": "old-id", "version": 2, "artifact_key": "game.html"}

    async def fake_insert(values, supersede_id):
        captured["values"] = values
        captured["supersede_id"] = supersede_id
        return values["id"]

    monkeypatch.setattr(artifacts, "_store_artifact_bytes", fake_store)
    monkeypatch.setattr(artifacts, "_current_artifact_row", fake_current)
    monkeypatch.setattr(artifacts, "_insert_artifact_version", fake_insert)

    await artifacts.save_artifact(
        "<h1>v3</h1>", kind="html", key="game.html",
        conversation_id="conv-1", org_id="default",
    )

    assert captured["values"]["version"] == 3
    assert captured["values"]["is_current"] is True
    assert captured["values"]["artifact_key"] == "game.html"
    # The previous current row is superseded so only one row stays current.
    assert captured["supersede_id"] == "old-id"


@pytest.mark.asyncio
async def test_save_artifact_without_key_is_v1_keyed_by_its_own_id(monkeypatch):
    from core import artifacts

    captured: dict = {}

    async def fake_store(artifact_id, raw, org_id, mime_type):
        return "minio://x"

    async def fake_current(org_id, scope_id, key):
        raise AssertionError("must not look up a current row when no key is given")

    async def fake_insert(values, supersede_id):
        captured["values"] = values
        captured["supersede_id"] = supersede_id
        return values["id"]

    monkeypatch.setattr(artifacts, "_store_artifact_bytes", fake_store)
    monkeypatch.setattr(artifacts, "_current_artifact_row", fake_current)
    monkeypatch.setattr(artifacts, "_insert_artifact_version", fake_insert)

    new_id = await artifacts.save_artifact("body", kind="markdown", org_id="default")

    assert captured["values"]["version"] == 1
    assert captured["values"]["artifact_key"] == new_id  # legacy rows are self-keyed
    assert captured["supersede_id"] is None


# ── agent artifact__* tools ─────────────────────────────────────────────────────

def _task() -> dict:
    return {
        "id": "task-2",
        "organization_id": "default",
        "region": "us",
        "depth": 0,
        # depth-0 task triggered by a conversation → conversation is the scope.
        "triggered_by": "11111111-1111-1111-1111-111111111111",
    }


@pytest.mark.asyncio
async def test_artifact_write_tool_versions_within_conversation(monkeypatch):
    from runtime import agent_loop
    from core import artifacts

    calls: dict = {}

    async def fake_save(content, *, kind, key=None, conversation_id=None,
                        task_id=None, org_id="default", region="us", title=None, **_):
        calls["key"] = key
        calls["conversation_id"] = conversation_id
        calls["content"] = content
        return "new-version-id"

    async def fake_get(artifact_id):
        assert artifact_id == "new-version-id"
        return {"id": artifact_id, "version": 2, "artifact_key": "game.html", "kind": "html"}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(artifacts, "save_artifact", fake_save)
    monkeypatch.setattr(artifacts, "get_artifact", fake_get)
    monkeypatch.setattr(agent_loop, "emit_activity", noop)

    call = {
        "id": "c1",
        "name": "artifact__write",
        "args_str": json.dumps({"key": "game.html", "content": "<h1>v2</h1>", "kind": "html"}),
    }
    msg = await agent_loop._execute_tool(call, _task(), agent=None)

    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "c1"
    body = json.loads(msg["content"])
    assert body["version"] == 2
    assert body["artifact_id"] == "new-version-id"
    # Scoped to the conversation so a later task can read/iterate it.
    assert calls["conversation_id"] == "11111111-1111-1111-1111-111111111111"
    assert calls["key"] == "game.html"


@pytest.mark.asyncio
async def test_artifact_read_tool_returns_current_content(monkeypatch):
    from runtime import agent_loop
    from core import artifacts

    async def fake_current(org_id, scope_id, key):
        assert key == "game.html"
        return {"id": "cur-id", "version": 2, "artifact_key": "game.html", "kind": "html"}

    async def fake_read(artifact_id):
        assert artifact_id == "cur-id"
        return b"<h1>current</h1>"

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(artifacts, "get_current_artifact", fake_current)
    monkeypatch.setattr(artifacts, "read_artifact_content", fake_read)
    monkeypatch.setattr(agent_loop, "emit_activity", noop)

    call = {"id": "c2", "name": "artifact__read", "args_str": json.dumps({"key": "game.html"})}
    msg = await agent_loop._execute_tool(call, _task(), agent=None)

    body = json.loads(msg["content"])
    assert body["content"] == "<h1>current</h1>"
    assert body["version"] == 2


@pytest.mark.asyncio
async def test_artifact_list_tool_lists_current_artifacts(monkeypatch):
    from runtime import agent_loop
    from core import artifacts

    async def fake_list(org_id, scope_id):
        return [
            {"id": "a", "artifact_key": "game.html", "title": "Game", "kind": "html", "version": 2},
            {"id": "b", "artifact_key": "notes.md", "title": "Notes", "kind": "markdown", "version": 1},
        ]

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(artifacts, "list_current_artifacts", fake_list)
    monkeypatch.setattr(agent_loop, "emit_activity", noop)

    call = {"id": "c3", "name": "artifact__list", "args_str": "{}"}
    msg = await agent_loop._execute_tool(call, _task(), agent=None)

    body = json.loads(msg["content"])
    assert {a["key"] for a in body["artifacts"]} == {"game.html", "notes.md"}
    assert any(a["version"] == 2 for a in body["artifacts"])
