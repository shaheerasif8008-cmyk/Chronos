import pytest


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Chunk:
    def __init__(self, delta):
        self.choices = [_Choice(delta)]


class _ToolCallFrag:
    def __init__(self, index, id=None, name=None, args=""):
        self.index = index
        self.id = id
        self.function = type("F", (), {"name": name, "arguments": args})()


async def _aiter(chunks):
    for c in chunks:
        yield c


@pytest.mark.asyncio
async def test_stream_step_relays_content_tokens(monkeypatch):
    from core import llm

    async def fake_acompletion(**kwargs):
        assert kwargs["stream"] is True
        return _aiter([_Chunk(_Delta(content="Hel")), _Chunk(_Delta(content="lo"))])

    monkeypatch.setattr(llm.litellm, "acompletion", fake_acompletion)

    tokens, events = [], []
    async for ev in llm.stream_step([{"role": "user", "content": "hi"}], [], "m"):
        events.append(ev)
        if ev["type"] == "token":
            tokens.append(ev["content"])

    assert "".join(tokens) == "Hello"
    assert events[-1] == {"type": "text_done", "text": "Hello"}


@pytest.mark.asyncio
async def test_stream_step_accumulates_tool_calls(monkeypatch):
    from core import llm

    async def fake_acompletion(**kwargs):
        return _aiter(
            [
                _Chunk(
                    _Delta(
                        tool_calls=[
                            _ToolCallFrag(
                                0, id="c1", name="browser__search", args='{"q'
                            )
                        ]
                    )
                ),
                _Chunk(_Delta(tool_calls=[_ToolCallFrag(0, args='uery":"x"}')])),
            ]
        )

    monkeypatch.setattr(llm.litellm, "acompletion", fake_acompletion)

    events = [
        ev
        async for ev in llm.stream_step(
            [{"role": "user", "content": "find x"}], [], "m"
        )
    ]

    assert not [e for e in events if e["type"] == "token"]
    final = events[-1]
    assert final["type"] == "tool_calls"
    assert final["calls"] == [
        {"id": "c1", "name": "browser__search", "args_str": '{"query":"x"}'}
    ]


@pytest.mark.asyncio
async def test_stream_step_fails_over_before_output_and_preserves_tools(monkeypatch):
    from core import llm

    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        if kwargs["model"].startswith("openrouter/"):
            raise RuntimeError("primary unavailable")
        return _aiter([_Chunk(_Delta(content="backup"))])

    monkeypatch.setattr(llm.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm.settings, "backup_api_key", "anthropic-key")
    monkeypatch.setattr(llm.settings, "backup_model", "anthropic/claude-sonnet-5")
    tools = [{"type": "function", "function": {"name": "browser__search"}}]

    events = [
        event
        async for event in llm.stream_step(
            [{"role": "user", "content": "find x"}],
            tools,
            "openrouter/openai/gpt-5.4-mini",
        )
    ]

    assert [call["model"] for call in calls] == [
        "openrouter/openai/gpt-5.4-mini",
        "anthropic/claude-sonnet-5",
    ]
    assert calls[1]["tools"] == tools
    assert events[-1] == {"type": "text_done", "text": "backup"}


@pytest.mark.asyncio
async def test_stream_step_never_replays_after_visible_output(monkeypatch):
    from core import llm

    calls = []

    async def broken_stream():
        yield _Chunk(_Delta(content="partial"))
        raise RuntimeError("connection lost")

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return broken_stream()

    monkeypatch.setattr(llm.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm.settings, "backup_api_key", "anthropic-key")
    monkeypatch.setattr(llm.settings, "backup_model", "anthropic/claude-sonnet-5")

    with pytest.raises(RuntimeError, match="connection lost"):
        async for _ in llm.stream_step(
            [{"role": "user", "content": "hi"}], [], "openrouter/openai/gpt-5.4-mini"
        ):
            pass

    assert len(calls) == 1
