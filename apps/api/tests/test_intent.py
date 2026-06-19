import pytest


@pytest.mark.asyncio
async def test_classify_intent_uses_llm(monkeypatch):
    from core import intent as intent_mod

    async def fake_complete_json(prompt, *, model=None):
        return '{"mode": "task", "goal": "research competitor pricing"}'

    monkeypatch.setattr(intent_mod, "complete_json", fake_complete_json)
    out = await intent_mod.classify_intent("look into how rivals price their plans")
    assert out == {"mode": "task", "goal": "research competitor pricing"}


@pytest.mark.asyncio
async def test_classify_intent_chat_mode_drops_goal(monkeypatch):
    from core import intent as intent_mod

    async def fake_complete_json(prompt, *, model=None):
        return '{"mode": "chat", "goal": "ignored"}'

    monkeypatch.setattr(intent_mod, "complete_json", fake_complete_json)
    out = await intent_mod.classify_intent("what is a client intake agent?")
    assert out == {"mode": "chat", "goal": None}


@pytest.mark.asyncio
async def test_classify_intent_falls_back_to_heuristic_on_llm_error(monkeypatch):
    from core import intent as intent_mod

    async def boom(prompt, *, model=None):
        raise RuntimeError("llm unavailable")

    monkeypatch.setattr(intent_mod, "complete_json", boom)
    task_out = await intent_mod.classify_intent("draft an email to the client")
    assert task_out["mode"] == "task"
    chat_out = await intent_mod.classify_intent("hello there")
    assert chat_out["mode"] == "chat"


@pytest.mark.asyncio
async def test_classify_intent_falls_back_on_malformed_json(monkeypatch):
    from core import intent as intent_mod

    async def garbage(prompt, *, model=None):
        return "not json at all"

    monkeypatch.setattr(intent_mod, "complete_json", garbage)
    out = await intent_mod.classify_intent("summarize this thread")
    # heuristic sees task verb "summarize"
    assert out["mode"] == "task"


@pytest.mark.asyncio
async def test_classify_intent_empty_message_is_chat():
    from core import intent as intent_mod

    out = await intent_mod.classify_intent("   ")
    assert out == {"mode": "chat", "goal": None}


def test_effort_for_difficulty_mapping():
    from core.intent import effort_for_difficulty

    assert effort_for_difficulty("trivial") is None
    assert effort_for_difficulty("simple") == "low"
    assert effort_for_difficulty("standard") == "medium"
    assert effort_for_difficulty("hard") == "high"
    assert effort_for_difficulty("nonsense") is None
    assert effort_for_difficulty(None) is None


@pytest.mark.asyncio
async def test_classify_request_threads_difficulty_into_effort(monkeypatch):
    from core import intent as intent_mod

    async def fake_complete_json(prompt, *, model=None):
        return '{"mode": "task", "difficulty": "hard", "goal": "design the schema"}'

    monkeypatch.setattr(intent_mod, "complete_json", fake_complete_json)
    out = await intent_mod.classify_request("design a multi-tenant schema with tradeoffs")
    assert out["mode"] == "task"
    assert out["difficulty"] == "hard"
    assert out["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_classify_request_recovers_difficulty_when_model_omits_it(monkeypatch):
    from core import intent as intent_mod

    async def fake_complete_json(prompt, *, model=None):
        # Model returns a valid mode but no difficulty field.
        return '{"mode": "chat", "goal": null}'

    monkeypatch.setattr(intent_mod, "complete_json", fake_complete_json)
    out = await intent_mod.classify_request("compare the tradeoffs between Postgres and DynamoDB for this")
    # "compare"/"tradeoffs" markers push the heuristic to hard → high effort.
    assert out["difficulty"] == "hard"
    assert out["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_classify_request_falls_back_on_error(monkeypatch):
    from core import intent as intent_mod

    async def boom(prompt, *, model=None):
        raise RuntimeError("llm down")

    monkeypatch.setattr(intent_mod, "complete_json", boom)
    out = await intent_mod.classify_request("hi")
    assert out["mode"] == "chat"
    assert out["difficulty"] == "trivial"
    assert out["reasoning_effort"] is None
