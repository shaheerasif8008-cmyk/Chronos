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
