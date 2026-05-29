import json

import pytest


@pytest.mark.asyncio
async def test_save_artifact_creates_v1_when_key_is_new(monkeypatch):
    from core import artifacts

    captured: dict = {}

    async def fake_store(artifact_id, raw, org_id, mime_type):
        return f"minio://{org_id}/{artifact_id}"

    async def fake_current(org_id, scope_id, key):
        return None

    async def fake_insert(values, supersede_id):
        captured["values"] = values
        captured["supersede_id"] = supersede_id
        return values["id"]

    monkeypatch.setattr(artifacts, "_store_artifact_bytes", fake_store)
    monkeypatch.setattr(artifacts, "_current_artifact_row", fake_current)
    monkeypatch.setattr(artifacts, "_insert_artifact_version", fake_insert)

    new_id = await artifacts.save_artifact(
        "<h1>v1</h1>",
        kind="html",
        key="demo.html",
        conversation_id="conv-1",
        org_id="default",
    )

    assert new_id == captured["values"]["id"]
    assert captured["values"]["artifact_key"] == "demo.html"
    assert captured["values"]["mime_type"] == "text/html"
    assert captured["values"]["version"] == 1
    assert captured["values"]["is_current"] is True
    assert captured["supersede_id"] is None


@pytest.mark.asyncio
async def test_save_artifact_bumps_version_and_supersedes_existing_key(monkeypatch):
    from core import artifacts

    captured: dict = {}

    async def fake_store(artifact_id, raw, org_id, mime_type):
        return "minio://x"

    async def fake_current(org_id, scope_id, key):
        assert scope_id == "conv-1"
        assert key == "demo.html"
        return {"id": "old-id", "version": 2}

    async def fake_insert(values, supersede_id):
        captured["values"] = values
        captured["supersede_id"] = supersede_id
        return values["id"]

    monkeypatch.setattr(artifacts, "_store_artifact_bytes", fake_store)
    monkeypatch.setattr(artifacts, "_current_artifact_row", fake_current)
    monkeypatch.setattr(artifacts, "_insert_artifact_version", fake_insert)

    await artifacts.save_artifact(
        "<h1>v3</h1>",
        kind="html",
        key="demo.html",
        conversation_id="conv-1",
        org_id="default",
    )

    assert captured["values"]["version"] == 3
    assert captured["values"]["artifact_key"] == "demo.html"
    assert captured["supersede_id"] == "old-id"


@pytest.mark.asyncio
async def test_save_artifact_without_key_stays_standalone(monkeypatch):
    from core import artifacts

    captured: dict = {}

    async def fake_store(artifact_id, raw, org_id, mime_type):
        return "minio://x"

    async def fake_current(org_id, scope_id, key):
        raise AssertionError("keyless artifacts must not query current rows")

    async def fake_insert(values, supersede_id):
        captured["values"] = values
        captured["supersede_id"] = supersede_id
        return values["id"]

    monkeypatch.setattr(artifacts, "_store_artifact_bytes", fake_store)
    monkeypatch.setattr(artifacts, "_current_artifact_row", fake_current)
    monkeypatch.setattr(artifacts, "_insert_artifact_version", fake_insert)

    new_id = await artifacts.save_artifact("body", kind="markdown", org_id="default")

    assert captured["values"]["version"] == 1
    assert captured["values"]["artifact_key"] == new_id
    assert captured["supersede_id"] is None


def _conversation_task() -> dict:
    return {
        "id": "22222222-2222-2222-2222-222222222222",
        "organization_id": "default",
        "region": "us",
        "depth": 0,
        "triggered_by": "11111111-1111-1111-1111-111111111111",
    }


@pytest.mark.asyncio
async def test_artifact_write_tool_versions_in_conversation(monkeypatch):
    from core import artifacts
    from runtime import agent_loop

    calls: dict = {}

    async def fake_save(content, *, key=None, conversation_id=None, task_id=None, **kwargs):
        calls.update({"content": content, "key": key, "conversation_id": conversation_id, "task_id": task_id})
        return "new-id"

    async def fake_get(artifact_id):
        return {"id": artifact_id, "artifact_key": "demo.html", "version": 2}

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(artifacts, "save_artifact", fake_save)
    monkeypatch.setattr(artifacts, "get_artifact", fake_get)
    monkeypatch.setattr(agent_loop, "emit_activity", noop)

    msg = await agent_loop._execute_tool(
        {
            "id": "call-1",
            "name": "artifact__write",
            "args_str": json.dumps({"key": "demo.html", "content": "<h1>v2</h1>", "kind": "html"}),
        },
        _conversation_task(),
        agent=None,
    )

    body = json.loads(msg["content"])
    assert body["artifact_id"] == "new-id"
    assert body["version"] == 2
    assert calls["key"] == "demo.html"
    assert calls["conversation_id"] == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_artifact_read_tool_returns_current_content(monkeypatch):
    from core import artifacts
    from runtime import agent_loop

    async def fake_current(org_id, scope_id, key):
        assert key == "demo.html"
        return {"id": "current-id", "artifact_key": key, "kind": "html", "version": 2}

    async def fake_read(artifact_id):
        return b"<h1>current</h1>"

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(artifacts, "get_current_artifact", fake_current)
    monkeypatch.setattr(artifacts, "read_artifact_content", fake_read)
    monkeypatch.setattr(agent_loop, "emit_activity", noop)

    msg = await agent_loop._execute_tool(
        {"id": "call-2", "name": "artifact__read", "args_str": json.dumps({"key": "demo.html"})},
        _conversation_task(),
        agent=None,
    )

    body = json.loads(msg["content"])
    assert body["version"] == 2
    assert body["content"] == "<h1>current</h1>"


@pytest.mark.asyncio
async def test_artifact_list_tool_lists_current_artifacts(monkeypatch):
    from core import artifacts
    from runtime import agent_loop

    async def fake_list(org_id, scope_id):
        return [
            {"id": "a", "artifact_key": "demo.html", "title": "Demo", "kind": "html", "version": 2},
            {"id": "b", "artifact_key": "notes.md", "title": "Notes", "kind": "markdown", "version": 1},
        ]

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(artifacts, "list_current_artifacts", fake_list)
    monkeypatch.setattr(agent_loop, "emit_activity", noop)

    msg = await agent_loop._execute_tool(
        {"id": "call-3", "name": "artifact__list", "args_str": "{}"},
        _conversation_task(),
        agent=None,
    )

    body = json.loads(msg["content"])
    assert {item["key"] for item in body["artifacts"]} == {"demo.html", "notes.md"}
    assert any(item["version"] == 2 for item in body["artifacts"])
