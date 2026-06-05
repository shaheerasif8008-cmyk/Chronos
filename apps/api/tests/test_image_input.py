"""Tests for Task 3: Image input / vision.

Covers:
- model_supports_vision registry helper
- build_user_turn_content / build_image_block pure helpers
- Multimodal user-turn wiring in stream_chat_turn (vision path)
- Degraded fallback (non-vision model): OCR text + truthful note, no image_url block
- Cross-org isolation: image artifact from another org must not be embedded
"""
from __future__ import annotations

import base64
import pytest


# ── 1. model_supports_vision registry ────────────────────────────────────────


def test_model_supports_vision_true_for_gpt_models():
    from core.llm import model_supports_vision

    assert model_supports_vision("gpt-5.4-mini") is True
    assert model_supports_vision("gpt-5.4-nano") is True


def test_model_supports_vision_false_for_deepseek_models():
    from core.llm import model_supports_vision

    assert model_supports_vision("deepseek-v4-pro") is False
    assert model_supports_vision("deepseek-v4-flash") is False


def test_model_supports_vision_false_for_unknown_model():
    from core.llm import model_supports_vision

    assert model_supports_vision("does-not-exist") is False
    assert model_supports_vision("") is False


# ── 2. build_image_block ──────────────────────────────────────────────────────


def test_build_image_block_produces_data_url():
    from core.context import build_image_block

    raw = b"\x89PNG\r\n\x1a\nfakeimagedata"
    block = build_image_block(raw, "image/png")

    assert block["type"] == "image_url"
    url = block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # Decode the base64 payload and verify it round-trips
    payload_b64 = url[len("data:image/png;base64,"):]
    decoded = base64.b64decode(payload_b64)
    assert decoded == raw


def test_build_image_block_jpeg():
    from core.context import build_image_block

    raw = b"\xff\xd8\xff\xe0fakejpeg"
    block = build_image_block(raw, "image/jpeg")

    url = block["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    decoded = base64.b64decode(url[len("data:image/jpeg;base64,"):])
    assert decoded == raw


# ── 3. build_user_turn_content ────────────────────────────────────────────────


def test_build_user_turn_content_vision_path_returns_list():
    from core.context import build_image_block, build_user_turn_content

    raw = b"fakepng"
    block = build_image_block(raw, "image/png")
    content = build_user_turn_content("What is in this image?", [block], vision_available=True)

    assert isinstance(content, list)
    # First block is the text
    assert content[0] == {"type": "text", "text": "What is in this image?"}
    # Second block is the image_url
    assert content[1]["type"] == "image_url"
    url = content[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    decoded = base64.b64decode(url[len("data:image/png;base64,"):])
    assert decoded == raw


def test_build_user_turn_content_no_images_returns_string():
    from core.context import build_user_turn_content

    content = build_user_turn_content("Hello", [], vision_available=True)
    assert content == "Hello"
    assert isinstance(content, str)


def test_build_user_turn_content_non_vision_model_returns_string_with_note():
    from core.context import _VISION_UNAVAILABLE_NOTE, build_image_block, build_user_turn_content

    raw = b"fakepng"
    block = build_image_block(raw, "image/png")
    content = build_user_turn_content(
        "What is this?",
        [block],
        vision_available=False,
        ocr_note=_VISION_UNAVAILABLE_NOTE,
    )

    assert isinstance(content, str)
    # No image_url in output
    assert "image_url" not in content
    # Truthful note is present — the note mentions "does not support" vision
    assert "does not support" in content.lower() or "vision unavailable" in content.lower()
    # Original message still present
    assert "What is this?" in content


def test_build_user_turn_content_non_vision_no_note_returns_plain_string():
    from core.context import build_user_turn_content

    content = build_user_turn_content("plain message", [], vision_available=False)
    assert content == "plain message"


# ── 4. stream_chat_turn wiring (vision path) ─────────────────────────────────


@pytest.mark.asyncio
async def test_stream_chat_turn_passes_list_content_to_model_when_vision_active(monkeypatch):
    """When user_content is a list (vision blocks), stream_chat_turn must pass that
    list as the user-turn content to the model, not the plain message string."""
    from runtime import agent_loop
    from core.context import build_image_block, build_user_turn_content

    raw = b"fakepng"
    block = build_image_block(raw, "image/png")
    user_content = build_user_turn_content("Describe the image", [block], vision_available=True)
    assert isinstance(user_content, list)

    captured_history: list = []

    async def fake_stream_step(messages, tools, model):
        captured_history.extend(messages)
        yield {"type": "text_done", "text": "A blue square."}

    async def fake_persist(conv_id, content, ctx, mode=None, **kwargs):
        pass

    async def fake_extract(*a, **k):
        return None

    monkeypatch.setattr(agent_loop, "stream_step", fake_stream_step)
    monkeypatch.setattr(agent_loop, "persist_assistant_message", fake_persist)
    monkeypatch.setattr(agent_loop, "extract_and_save", fake_extract)

    from core.models import RequesterContext
    ctx = RequesterContext(org_id="default", member_id="m1", role="user")

    events = [
        ev async for ev in agent_loop.stream_chat_turn(
            conversation_id="conv-1",
            message="Describe the image",
            context_messages=[{"role": "system", "content": "sys"}],
            requester_context=ctx,
            model="gpt-5.4-mini",
            user_content=user_content,
        )
    ]

    # The last user-role message in the history captured by stream_step must be
    # our list content (with an image_url block), not the plain string.
    user_turns = [m for m in captured_history if m.get("role") == "user"]
    assert user_turns, "No user turn found in captured history"
    last_user = user_turns[-1]
    assert isinstance(last_user["content"], list), (
        f"Expected list user-turn content, got: {type(last_user['content'])}"
    )
    image_url_blocks = [b for b in last_user["content"] if b.get("type") == "image_url"]
    assert image_url_blocks, "No image_url block found in user turn"
    url = image_url_blocks[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    decoded = base64.b64decode(url[len("data:image/png;base64,"):])
    assert decoded == raw

    # Done event emitted
    assert any(ev.get("type") == "done" for ev in events)


@pytest.mark.asyncio
async def test_stream_chat_turn_no_user_content_uses_plain_message(monkeypatch):
    """When user_content is not provided, the plain message string is used (regression guard)."""
    from runtime import agent_loop

    captured_history: list = []

    async def fake_stream_step(messages, tools, model):
        captured_history.extend(messages)
        yield {"type": "text_done", "text": "ok"}

    async def fake_persist(conv_id, content, ctx, mode=None, **kwargs):
        pass

    async def fake_extract(*a, **k):
        return None

    monkeypatch.setattr(agent_loop, "stream_step", fake_stream_step)
    monkeypatch.setattr(agent_loop, "persist_assistant_message", fake_persist)
    monkeypatch.setattr(agent_loop, "extract_and_save", fake_extract)

    from core.models import RequesterContext
    ctx = RequesterContext(org_id="default", member_id="m1", role="user")

    async def _collect():
        return [
            ev async for ev in agent_loop.stream_chat_turn(
                conversation_id="conv-1",
                message="plain text message",
                context_messages=[{"role": "system", "content": "sys"}],
                requester_context=ctx,
                model="gpt-5.4-mini",
            )
        ]

    await _collect()

    user_turns = [m for m in captured_history if m.get("role") == "user"]
    assert user_turns
    last_user = user_turns[-1]
    assert last_user["content"] == "plain text message"
    assert isinstance(last_user["content"], str)


# ── 5. Degraded fallback: non-vision model + images → OCR note, no image_url ─


def test_degraded_fallback_no_image_url_block():
    """Non-vision model: build_user_turn_content must NOT produce any image_url block."""
    from core.context import _VISION_UNAVAILABLE_NOTE, build_image_block, build_user_turn_content

    raw = b"fakepng"
    block = build_image_block(raw, "image/png")
    content = build_user_turn_content(
        "describe this",
        [block],
        vision_available=False,
        ocr_note=_VISION_UNAVAILABLE_NOTE,
    )
    assert isinstance(content, str)
    assert "image_url" not in content
    assert "data:image/png" not in content


def test_degraded_fallback_contains_truthful_note():
    from core.context import _VISION_UNAVAILABLE_NOTE, build_image_block, build_user_turn_content

    raw = b"fakepng"
    block = build_image_block(raw, "image/png")
    content = build_user_turn_content(
        "describe this",
        [block],
        vision_available=False,
        ocr_note=_VISION_UNAVAILABLE_NOTE,
    )
    # The note must mention vision being unavailable and OCR
    assert "vision unavailable" in content.lower() or "does not support" in content.lower()
    assert "ocr" in content.lower()


# ── 6. Cross-org: foreign org artifact must not be embedded ──────────────────


def _patch_artifact_store(monkeypatch, store: dict[str, tuple[dict, bytes]]):
    """Patch the chat router's module-global artifact accessors with a fixture store.

    ``store`` maps artifact_id -> (metadata dict, raw bytes). This drives the REAL
    ``_build_vision_blocks`` helper rather than re-implementing its filter inline.
    """
    import routers.chat as chat_router

    async def fake_get(att_id):
        entry = store.get(att_id)
        return entry[0] if entry else None

    async def fake_read(att_id):
        entry = store.get(att_id)
        return entry[1] if entry else b""

    monkeypatch.setattr(chat_router, "_get_artifact", fake_get)
    monkeypatch.setattr(chat_router, "_read_artifact_content", fake_read)


@pytest.mark.asyncio
async def test_cross_org_image_not_embedded(monkeypatch):
    """An image artifact from a different org must not produce image blocks.

    Drives the real ``_build_vision_blocks`` helper (the actual router guard) — not an
    inline re-implementation — so deleting the org filter would fail this test.
    """
    import routers.chat as chat_router

    member_org = "org-alpha"
    store = {
        "foreign": ({"organization_id": "org-beta", "mime_type": "image/png", "kind": "attachment"}, b"\x89PNG-foreign"),
    }
    _patch_artifact_store(monkeypatch, store)

    image_blocks, has_images = await chat_router._build_vision_blocks(
        ["foreign"], member_org, "gpt-5.4-mini"
    )
    assert image_blocks == [], "Foreign-org image must not produce image blocks"
    assert has_images is False, "Foreign-org image must not even count as a present image"


@pytest.mark.asyncio
async def test_same_org_image_is_embedded(monkeypatch):
    """An image from the same org, with a vision model, is embedded via the real helper."""
    import routers.chat as chat_router

    member_org = "org-alpha"
    store = {
        "own": ({"organization_id": member_org, "mime_type": "image/png", "kind": "attachment"}, b"\x89PNG-own-bytes"),
    }
    _patch_artifact_store(monkeypatch, store)

    image_blocks, has_images = await chat_router._build_vision_blocks(
        ["own"], member_org, "gpt-5.4-mini"
    )
    assert has_images is True
    assert len(image_blocks) == 1
    assert image_blocks[0]["type"] == "image_url"
    # The data URL must encode the real stored bytes.
    import base64
    data_url = image_blocks[0]["image_url"]["url"]
    b64 = data_url.split(",", 1)[1]
    assert base64.b64decode(b64) == b"\x89PNG-own-bytes"


@pytest.mark.asyncio
async def test_non_vision_model_has_images_but_no_blocks(monkeypatch):
    """A non-vision model with a same-org image: has_images True, but no blocks built
    (so the router takes the honest OCR degraded path)."""
    import routers.chat as chat_router

    member_org = "org-alpha"
    store = {
        "own": ({"organization_id": member_org, "mime_type": "image/png", "kind": "attachment"}, b"img"),
    }
    _patch_artifact_store(monkeypatch, store)

    image_blocks, has_images = await chat_router._build_vision_blocks(
        ["own"], member_org, "deepseek-v4-pro"
    )
    assert has_images is True
    assert image_blocks == []


@pytest.mark.asyncio
async def test_oversized_image_skipped_from_vision_blocks(monkeypatch):
    """An image exceeding the per-image byte cap is not embedded (avoids provider
    body-limit failures), though it still counts as a present image."""
    import routers.chat as chat_router

    member_org = "org-alpha"
    big = b"x" * (chat_router._MAX_IMAGE_BYTES_VISION + 1)
    store = {
        "big": ({"organization_id": member_org, "mime_type": "image/png", "kind": "attachment"}, big),
    }
    _patch_artifact_store(monkeypatch, store)

    image_blocks, has_images = await chat_router._build_vision_blocks(
        ["big"], member_org, "gpt-5.4-mini"
    )
    assert has_images is True
    assert image_blocks == [], "Oversized image must be skipped from inline vision blocks"
