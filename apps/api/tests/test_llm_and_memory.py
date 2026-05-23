import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from core.models import RequesterContext


@pytest.mark.asyncio
async def test_stream_chat_completion_defaults_to_agent_model(monkeypatch):
    from core import llm

    calls = []

    async def fake_completion(**kwargs):
        calls.append(kwargs)

        async def chunks():
            yield {"choices": [{"delta": {"content": "agent "}}]}
            yield {"choices": [{"delta": {"content": "works"}}]}

        return chunks()

    monkeypatch.setattr(llm.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(llm.settings, "openrouter_api_key", "or-test-key")
    monkeypatch.setattr(llm.settings, "agent_model", "openrouter/deepseek/deepseek-v4-pro")

    tokens = []
    async for token in llm.stream_completion([{"role": "user", "content": "hi"}]):
        tokens.append(token)

    assert tokens == ["agent ", "works"]
    assert len(calls) == 1
    assert calls[0]["api_key"] == "or-test-key"
    assert calls[0]["api_base"] == "https://openrouter.ai/api/v1"
    assert calls[0]["model"] == "openrouter/deepseek/deepseek-v4-pro"


@pytest.mark.asyncio
async def test_stream_chat_completion_uses_selected_fast_model(monkeypatch):
    from core import llm

    calls = []

    async def fake_completion(**kwargs):
        calls.append(kwargs)

        async def chunks():
            yield {"choices": [{"delta": {"content": "fast"}}]}

        return chunks()

    monkeypatch.setattr(llm.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(llm.settings, "fast_model", "openrouter/minimax/minimax-m2.5:free")
    monkeypatch.setattr(llm.settings, "openrouter_api_key", "or-test-key")

    tokens = []
    async for token in llm.stream_completion([{"role": "user", "content": "hi"}], model_id="fast"):
        tokens.append(token)

    assert tokens == ["fast"]
    assert len(calls) == 1
    assert calls[0]["model"] == "openrouter/minimax/minimax-m2.5:free"
    assert calls[0]["api_key"] == "or-test-key"


def test_available_chat_models_include_configured_options(monkeypatch):
    from core import llm

    monkeypatch.setattr(llm.settings, "local_llm_model", "llama3")
    monkeypatch.setattr(llm.settings, "openrouter_api_key", "or-test-key")
    monkeypatch.setattr(llm.settings, "openrouter_model", "openrouter/example/model")
    monkeypatch.setattr(llm.settings, "agent_model", "openrouter/example/agent")
    monkeypatch.setattr(llm.settings, "fast_model", "openrouter/example/fast")

    models = llm.available_chat_models()

    assert [model["id"] for model in models] == ["agent", "auto", "local", "openrouter", "fast"]
    assert llm.normalize_chat_model(None) == "agent"
    assert llm.normalize_chat_model("agent") == "agent"
    assert llm.normalize_chat_model("openrouter") == "openrouter"
    assert llm.normalize_chat_model("does-not-exist") == "agent"


@pytest.mark.asyncio
async def test_stream_chat_completion_reports_provider_unavailable(monkeypatch):
    from core import llm

    async def fake_completion(**kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(llm.litellm, "acompletion", fake_completion)

    tokens = []
    async for token in llm.stream_completion([{"role": "user", "content": "hi"}]):
        tokens.append(token)

    assert tokens == [
        "Chronos is connected, but the selected AI provider is unavailable right now. Try Auto or another model."
    ]


@pytest.mark.asyncio
async def test_complete_json_falls_back_to_main_model_after_fast_failure(monkeypatch):
    from core import llm

    calls = []

    async def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise llm.litellm.RateLimitError(
                message="temporarily rate-limited upstream",
                llm_provider="openrouter",
                model=kwargs["model"],
            )
        return {"choices": [{"message": {"content": '{"mode":"chat"}'}}]}

    monkeypatch.setattr(llm.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(llm.settings, "fast_model", "openrouter/minimax/minimax-m2.5:free")
    monkeypatch.setattr(llm.settings, "agent_model", "openrouter/deepseek/deepseek-v4-pro")
    monkeypatch.setattr(llm.settings, "openrouter_api_key", "or-test-key")

    result = await llm.complete_json("Return JSON")

    assert result == '{"mode":"chat"}'
    assert calls[0]["model"] == "openrouter/minimax/minimax-m2.5:free"
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[1]["model"] == "openrouter/deepseek/deepseek-v4-pro"


@pytest.mark.asyncio
async def test_embed_uses_redis_cache(monkeypatch):
    from core import embeddings

    store = {}
    class FakeRedis:
        async def get(self, key):
            return store.get(key)

        async def setex(self, key, ttl, value):
            store[key] = value
            store[f"{key}:ttl"] = ttl

    monkeypatch.setattr(embeddings, "_redis", FakeRedis())
    kwargs = {}
    async def fake_openrouter_embedding(text):
        kwargs["model"] = embeddings.settings.embedding_model
        kwargs["api_key"] = embeddings.settings.openrouter_api_key
        kwargs["api_base"] = embeddings.settings.openrouter_api_base
        return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    monkeypatch.setattr(embeddings, "_openrouter_embedding", fake_openrouter_embedding)
    monkeypatch.setattr(embeddings.settings, "openrouter_api_key", "or-test-key")

    first = await embeddings.embed("remember this")
    second = await embeddings.embed("remember this")

    assert first == [0.1, 0.2, 0.3]
    assert second == first
    assert kwargs["model"] == "openrouter/nvidia/llama-nemotron-embed-vl-1b-v2:free"
    assert kwargs["api_key"] == "or-test-key"
    assert kwargs["api_base"] == "https://openrouter.ai/api/v1"
    assert 86400 in store.values()


@pytest.mark.asyncio
async def test_extract_and_save_filters_embeds_and_publishes(monkeypatch):
    from memory import extraction

    saved = []
    published = []

    class FakeRedis:
        async def publish(self, channel, payload):
            published.append((channel, json.loads(payload)))

    async def fake_complete_json(prompt, model=None):
        assert model == extraction.settings.fast_model
        return json.dumps(
            {
                "memories": [
                    {"content": "Keep this", "scope": "org", "importance": 0.8},
                    {"content": "Ignore this", "scope": "org", "importance": 0.2},
                ]
            }
        )

    async def fake_create_memory_entry(**kwargs):
        saved.append(kwargs)
        return "memory-1"

    monkeypatch.setattr(extraction, "_redis", FakeRedis())
    monkeypatch.setattr(extraction, "complete_json", fake_complete_json)
    monkeypatch.setattr(extraction, "create_memory_entry", fake_create_memory_entry)

    await extraction.extract_and_save(
        "conversation-1",
        "user",
        "assistant",
        RequesterContext(org_id="default", member_id="member-1"),
    )

    assert len(saved) == 1
    assert saved[0]["content"] == "Keep this"
    assert saved[0]["source"] == "autonomous"
    assert saved[0]["conversation_id"] == "conversation-1"
    assert saved[0]["created_by"] == "chronos"
    assert published[0][0] == "memories:conversation-1"
    assert published[0][1]["type"] == "memory_saved"


def test_extract_explicit_memory_content_handles_supported_phrases():
    from core.memory_writes import extract_explicit_memory_content

    assert extract_explicit_memory_content("remember that ACME uses HubSpot") == "ACME uses HubSpot"
    assert extract_explicit_memory_content("Please remember: Alex hates pricing-first outbound") == "Alex hates pricing-first outbound"
    assert extract_explicit_memory_content("what do you remember?") is None


@pytest.mark.asyncio
async def test_intent_classification_uses_fast_model(monkeypatch):
    from core import intent

    async def fake_complete_json(prompt, model=None):
        assert model == intent.settings.fast_model
        return json.dumps({"mode": "task", "confidence": 0.91, "goal": "research a market"})

    monkeypatch.setattr(intent, "complete_json", fake_complete_json)

    classified = await intent.classify_intent("Can you research a market?")

    assert classified["mode"] == "task"
    assert classified["confidence"] == 0.91


@pytest.mark.asyncio
async def test_skill_selection_uses_fast_model(monkeypatch):
    from skills import loader

    async def fake_complete_json(prompt, model=None):
        assert model == loader.settings.fast_model
        return json.dumps({"relevant_skill_ids": ["general"]})

    monkeypatch.setattr(loader, "load_skill_index", lambda: [{"id": "general", "name": "General", "description": "general help"}])
    monkeypatch.setattr(loader, "complete_json", fake_complete_json)

    assert await loader.find_relevant_skills("general help") == ["general"]


def test_memory_scope_authorization_and_hybrid_rerank():
    from core import memory

    context = RequesterContext(
        org_id="default",
        member_id="member-a",
        workspace_id="workspace-1",
        persona_id="persona-1",
        role="user",
    )

    pairs = memory._authorized_scope_pairs(context)
    assert ("org", "default") in pairs
    assert ("workspace", "workspace-1") in pairs
    assert ("persona", "persona-1") in pairs
    assert ("personal", "member-a") in pairs
    assert ("personal", "member-b") not in pairs

    now = datetime.now(timezone.utc)
    rows = [
        {"id": "close-stale", "distance": 0.05, "importance_score": 0.1, "created_at": now - timedelta(days=365)},
        {"id": "important-recent", "distance": 0.20, "importance_score": 1.0, "created_at": now},
    ]
    ranked = memory._rank_memory_rows(rows, now=now)
    assert ranked[0]["id"] == "important-recent"


@pytest.mark.asyncio
async def test_filesystem_connector_jails_task_workspace(tmp_path, monkeypatch):
    from connectors import filesystem

    monkeypatch.setattr(filesystem, "WORKSPACE_ROOT", tmp_path)

    write = await filesystem.filesystem_connector.execute(
        "fs.write",
        {"path": "notes/result.txt", "content": "hello", "__org_id": "default", "__task_id": "task-1"},
    )
    assert write.data["bytes"] == 5

    read = await filesystem.filesystem_connector.execute(
        "fs.read",
        {"path": "notes/result.txt", "__org_id": "default", "__task_id": "task-1"},
    )
    assert read.data["content"] == "hello"

    with pytest.raises(ValueError, match="escapes"):
        await filesystem.filesystem_connector.execute(
            "fs.read",
            {"path": "../secret.txt", "__org_id": "default", "__task_id": "task-1"},
        )


@pytest.mark.asyncio
async def test_code_connector_runs_restricted_python(tmp_path, monkeypatch):
    from connectors import code as code_connector_module

    monkeypatch.setattr(code_connector_module, "WORKSPACE_ROOT", tmp_path)

    result = await code_connector_module.code_connector.execute(
        "code.python",
        {"code": "print(sum([1, 2, 3]))", "__org_id": "default", "__task_id": "task-1"},
    )
    assert result.data["status"] == "success"
    assert result.data["stdout"].strip() == "6"

    with pytest.raises(ValueError, match="unsafe"):
        await code_connector_module.code_connector.execute(
            "code.python",
            {"code": "import socket\nprint('no')", "__org_id": "default", "__task_id": "task-1"},
        )


@pytest.mark.asyncio
async def test_mcp_discovery_uses_real_stdio_protocol(tmp_path):
    from connectors.framework.mcp import MCPDiscoveryService
    from connectors.framework.repository import InMemoryConnectorRepository

    server_script = tmp_path / "mcp_server.py"
    server_script.write_text(
        r'''
import json, sys

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if not length:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode())

def send(payload):
    body = json.dumps(payload).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()

while True:
    msg = read_message()
    if msg is None:
        break
    if msg.get("method") == "notifications/initialized":
        continue
    if msg.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {"tools": {}}}})
    elif msg.get("method") == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}})
    else:
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
''',
        encoding="utf-8",
    )
    repo = InMemoryConnectorRepository()
    server = await repo.register_mcp_server(
        tenant_id="default",
        name="Local Echo",
        transport="local",
        command=f"{sys.executable} {server_script}",
    )

    result = await MCPDiscoveryService(repo).discover(server["id"], tenant_id="default")

    assert result["status"] == "healthy"
    assert result["tools_discovered"] == 1
    assert result["tools"][0]["name"] == "echo"


def test_memory_router_has_audit_available_for_mutations():
    from routers import memory

    assert memory.audit.log


@pytest.mark.asyncio
async def test_connector_proof_uses_internal_echo_runtime(monkeypatch):
    from core.models import Member
    from routers import connectors
    from connectors.framework.repository import InMemoryConnectorRepository

    repository = InMemoryConnectorRepository()

    def fake_repo():
        return repository

    monkeypatch.setattr(connectors, "repo", fake_repo)

    result = await connectors.execute_connector_proof(
        connector_id="connector-1",
        provider="internal",
        member=Member(id="member-1", organization_id="default", email="admin@example.com"),
    )

    assert result == {"status": "success", "detail": {"message": "Chronos connector proof"}, "tool": "internal_echo.echo"}
    logs = await repository.list_execution_logs(tenant_id="default", connector_id="internal_echo")
    assert logs[0]["result_status"] == "success"


@pytest.mark.asyncio
async def test_tool_broker_audits_gmail_draft_without_logging_raw_args(monkeypatch):
    from connectors import registry
    from core import permissions, tool_broker
    from core.models import AgentContext, ToolResult

    audit_events = []

    class ConnectorRecord:
        provider = "gmail"
        vault_ref = "vlt_safe_ref"

    async def fake_permission(*args, **kwargs):
        return True

    async def fake_registry_get(agent, tool):
        return ConnectorRecord()

    async def noop_rate_limit(org_id):
        return None

    async def noop_loop(org_id, tool, args_hash):
        return None

    async def fake_route(agent, tool, args, vault_ref, tier):
        assert tool == "gmail.draft"
        assert args["body"] == "Sensitive draft body"
        assert vault_ref == "vlt_safe_ref"
        assert tier == "live"
        return ToolResult(data={"id": "draft-1"}, summary="Draft created: draft-1")

    async def fake_audit_log(*args, **kwargs):
        audit_events.append((args, kwargs))
        return "audit-1"

    async def fake_tool_policy(org_id, provider):
        return {"enabled": True, "approval_required": False}

    async def fake_connector_tier(provider):
        return "live"

    monkeypatch.setattr(permissions, "check", fake_permission)
    monkeypatch.setattr(tool_broker, "_check_rate_limit", noop_rate_limit)
    monkeypatch.setattr(tool_broker, "_check_loop", noop_loop)
    monkeypatch.setattr(tool_broker, "tool_policy", fake_tool_policy)
    monkeypatch.setattr(tool_broker, "connector_tier", fake_connector_tier)
    monkeypatch.setattr(registry, "get", fake_registry_get)
    monkeypatch.setattr(tool_broker, "_route", fake_route)
    monkeypatch.setattr(tool_broker.audit, "log", fake_audit_log)

    result = await tool_broker.execute(
        AgentContext(id="agent-1", org_id="default", member_id="member-1"),
        "gmail.draft",
        {"to": "client@example.com", "subject": "Proof", "body": "Sensitive draft body"},
    )

    assert result.summary == "Draft created: draft-1"
    assert audit_events[0][0][0] == "tool_call"
    assert audit_events[0][0][2] == "gmail.draft"
    assert "args_hash" in audit_events[0][1]["payload"]
    assert "Sensitive draft body" not in str(audit_events[0][1]["payload"])
    assert audit_events[1][0][0] == "tool_result"
    assert audit_events[1][1]["payload"] == {"summary": "Draft created: draft-1"}


@pytest.mark.asyncio
async def test_tool_broker_blocks_disabled_tool_from_settings(monkeypatch):
    from core import permissions, tool_broker
    from core.exceptions import ApprovalRequired
    from core.models import AgentContext

    async def fake_permission(*args, **kwargs):
        return True

    async def noop_rate_limit(org_id):
        return None

    async def noop_loop(org_id, tool, args_hash):
        return None

    async def fake_tool_policy(org_id, provider):
        assert provider == "browser"
        return {"enabled": False, "approval_required": False}

    async def fake_audit_log(*args, **kwargs):
        return "audit-1"

    monkeypatch.setattr(permissions, "check", fake_permission)
    monkeypatch.setattr(tool_broker, "_check_rate_limit", noop_rate_limit)
    monkeypatch.setattr(tool_broker, "_check_loop", noop_loop)
    monkeypatch.setattr(tool_broker, "tool_policy", fake_tool_policy)
    monkeypatch.setattr(tool_broker.audit, "log", fake_audit_log)

    with pytest.raises(ApprovalRequired, match="tool is disabled in settings"):
        await tool_broker.execute(
            AgentContext(id="agent-1", org_id="default", member_id="member-1"),
            "browser.fetch",
            {"url": "https://example.com"},
        )


def test_apply_context_suggestion_appends_patch_to_org_context(tmp_path):
    from routers.context import apply_context_patch

    org_path = tmp_path / "context" / "default" / "org.md"
    org_path.parent.mkdir(parents=True)
    org_path.write_text("# Org\nExisting fact.\n")

    result = apply_context_patch(org_path, "- New approved fact.")

    assert result == "# Org\nExisting fact.\n\n- New approved fact.\n"
    assert org_path.read_text() == result


@pytest.mark.asyncio
async def test_memory_embedding_literal_returns_none_for_wrong_dimension(monkeypatch):
    from core import memory_writes

    audit_events = []

    async def fake_embed(content):
        return [0.1] * 2048

    async def fake_audit_log(*args, **kwargs):
        audit_events.append((args, kwargs))

    monkeypatch.setattr(memory_writes, "embed", fake_embed)
    monkeypatch.setattr(memory_writes.audit, "log", fake_audit_log)

    literal = await memory_writes.embedding_literal_for_memory(
        "i have a dog",
        actor_id="member-1",
        action="memory.explicit",
    )

    assert literal is None
    assert audit_events[0][0][0] == "memory_embedding_skipped"
    assert audit_events[0][1]["decision"] == "dimension_mismatch"


@pytest.mark.asyncio
async def test_memory_retrieve_falls_back_to_recent_memories_on_embedding_dimension_mismatch(monkeypatch):
    from core import memory
    from core.models import RequesterContext

    audit_events = []

    async def fake_embed(query):
        return [0.1] * 2048

    async def fake_recent(requester_context, *, decision):
        assert requester_context.org_id == "default"
        assert decision == "dimension_mismatch"
        return [
            memory.MemoryEntry(
                id="memory-1",
                organization_id="default",
                region="us",
                content="i have a dog",
                scope="org",
                scope_id="default",
                source="explicit",
            )
        ]

    async def fake_audit_log(*args, **kwargs):
        audit_events.append((args, kwargs))

    monkeypatch.setattr(memory, "embed", fake_embed)
    monkeypatch.setattr(memory, "_retrieve_recent_memories", fake_recent)
    monkeypatch.setattr(memory.audit, "log", fake_audit_log)

    results = await memory.retrieve("what do you remember about my pet?", RequesterContext(member_id="member-1"))

    assert [entry.content for entry in results] == ["i have a dog"]
    assert audit_events[0][1]["decision"] == "dimension_mismatch"


@pytest.mark.asyncio
async def test_assemble_context_loads_persona_skills_memories_and_task_state(monkeypatch):
    from core import context
    from core.models import MemoryEntry, RequesterContext

    async def fake_org_context(org_id):
        return "Org fact."

    async def fake_find_skills(message):
        return ["sdr-outreach"]

    async def fake_load_skill(skill_id):
        return "Use outbound research workflow."

    async def fake_retrieve(message, requester_context):
        return [
            MemoryEntry(
                id="memory-1",
                organization_id="default",
                content="ACME uses HubSpot.",
                source="explicit",
            )
        ]

    async def fake_task_context(task_id):
        return "Goal: draft outreach\nStatus: running\nStep: 1/3"

    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return []

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, stmt):
            return FakeResult()

    class FakeEngine:
        def begin(self):
            return FakeConn()

    class FakeColumn:
        def __eq__(self, other):
            return ("eq", other)

        def desc(self):
            return ("desc", self)

    class FakeMessages:
        class c:
            role = FakeColumn()
            content = FakeColumn()
            conversation_id = FakeColumn()
            created_at = FakeColumn()

    async def fake_reflect_table(name):
        return FakeMessages()

    class FakeSelect:
        def where(self, *args):
            return self

        def order_by(self, *args):
            return self

        def limit(self, *args):
            return self

    monkeypatch.setattr(context, "load_org_context", fake_org_context)
    monkeypatch.setattr(context, "find_relevant_skills", fake_find_skills)
    monkeypatch.setattr(context, "load_skill_content", fake_load_skill)
    monkeypatch.setattr(context.memory, "retrieve", fake_retrieve)
    monkeypatch.setattr(context, "_load_task_context", fake_task_context)
    monkeypatch.setattr(context, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(context, "engine", FakeEngine())
    monkeypatch.setattr(context, "select", lambda *args: FakeSelect())

    assembled = await context.assemble_context(
        "conversation-1",
        "draft outreach to leads",
        RequesterContext(member_id="member-1", persona_id="sdr-outreach", task_id="task-1"),
    )

    system = assembled[0]["content"]
    assert "# Organization Context\nOrg fact." in system
    assert "# Your Identity\nYou research leads" in system
    assert "# Skill: sdr-outreach\nUse outbound research workflow." in system
    assert "# What I Remember\n- ACME uses HubSpot." in system
    assert "# Current Task\nGoal: draft outreach" in system
