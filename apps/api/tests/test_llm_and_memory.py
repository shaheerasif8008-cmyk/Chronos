import json

import pytest

from core.models import RequesterContext


@pytest.mark.asyncio
async def test_stream_chat_completion_falls_back_after_local_failure(monkeypatch):
    from core import llm

    calls = []

    async def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("local unavailable")

        async def chunks():
            yield {"choices": [{"delta": {"content": "fallback "}}]}
            yield {"choices": [{"delta": {"content": "works"}}]}

        return chunks()

    monkeypatch.setattr(llm.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(llm.settings, "openrouter_api_key", "or-test-key")
    monkeypatch.setattr(llm.settings, "openrouter_model", "openrouter/nvidia/nemotron-3-super-120b-a12b:free")

    tokens = []
    async for token in llm.stream_completion([{"role": "user", "content": "hi"}]):
        tokens.append(token)

    assert tokens == ["fallback ", "works"]
    assert calls[0]["api_base"] == llm.settings.local_llm_base_url
    assert calls[0]["model"] == llm.settings.local_llm_model
    assert calls[1]["api_key"] == "or-test-key"
    assert calls[1]["api_base"] == "https://openrouter.ai/api/v1"
    assert calls[1]["model"] == "openrouter/nvidia/nemotron-3-super-120b-a12b:free"


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
        "Chronos is connected, but the AI provider is unavailable right now. The local runtime, memory, task, approval, and connector tools are still available."
    ]


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

    async def fake_complete_json(prompt):
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


def test_memory_router_has_audit_available_for_mutations():
    from routers import memory

    assert memory.audit.log


@pytest.mark.asyncio
async def test_connector_proof_routes_gmail_draft_through_tool_broker(monkeypatch):
    from core.models import Member, ToolResult
    from routers import connectors

    calls = []

    async def fake_execute(agent, tool, args):
        calls.append((agent, tool, args))
        return ToolResult(data={"id": "draft-1"}, summary="Draft created: draft-1")

    async def noop_mark(connector_id):
        return None

    async def noop_audit(**kwargs):
        return None

    monkeypatch.setattr(connectors.tool_broker, "execute", fake_execute)
    monkeypatch.setattr(connectors, "_mark_connector_used", noop_mark)
    monkeypatch.setattr(connectors, "_audit_connector_proof", noop_audit)

    result = await connectors.execute_connector_proof(
        connector_id="connector-1",
        provider="gmail",
        member=Member(id="member-1", organization_id="default", email="admin@example.com"),
    )

    assert result == {"status": "ok", "detail": "Draft created: draft-1", "tool": "gmail.draft"}
    assert calls[0][1] == "gmail.draft"
    assert calls[0][2]["subject"] == "Chronos connector proof"


@pytest.mark.asyncio
async def test_connector_proof_routes_browser_fetch_through_tool_broker(monkeypatch):
    from core.models import Member, ToolResult
    from routers import connectors

    calls = []

    async def fake_execute(agent, tool, args):
        calls.append((agent, tool, args))
        return ToolResult(data={"title": "Example Domain"}, summary="Fetched https://example.com: 120 chars")

    async def noop_mark(connector_id):
        return None

    async def noop_audit(**kwargs):
        return None

    monkeypatch.setattr(connectors.tool_broker, "execute", fake_execute)
    monkeypatch.setattr(connectors, "_mark_connector_used", noop_mark)
    monkeypatch.setattr(connectors, "_audit_connector_proof", noop_audit)

    result = await connectors.execute_connector_proof(
        connector_id="connector-1",
        provider="browser",
        member=Member(id="member-1", organization_id="default", email="admin@example.com"),
    )

    assert result == {"status": "ok", "detail": "Fetched https://example.com: 120 chars", "tool": "browser.fetch"}
    assert calls[0][1] == "browser.fetch"
    assert calls[0][2] == {"url": "https://example.com"}


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

    async def fake_route(tool, args, vault_ref):
        assert tool == "gmail.draft"
        assert args["body"] == "Sensitive draft body"
        assert vault_ref == "vlt_safe_ref"
        return ToolResult(data={"id": "draft-1"}, summary="Draft created: draft-1")

    async def fake_audit_log(*args, **kwargs):
        audit_events.append((args, kwargs))
        return "audit-1"

    async def fake_tool_policy(org_id, provider):
        return {"enabled": True, "approval_required": False}

    monkeypatch.setattr(permissions, "check", fake_permission)
    monkeypatch.setattr(tool_broker, "_check_rate_limit", noop_rate_limit)
    monkeypatch.setattr(tool_broker, "_check_loop", noop_loop)
    monkeypatch.setattr(tool_broker, "tool_policy", fake_tool_policy)
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
