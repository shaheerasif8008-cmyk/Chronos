from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
from PIL import Image


_png_output = io.BytesIO()
Image.new("RGB", (1, 1), color=(0, 0, 0)).save(_png_output, format="PNG")
_PNG = _png_output.getvalue()


class _PostClient:
    def __init__(self, responses: list[httpx.Response], calls: list[dict]):
        self._responses = responses
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, headers, json):
        self._calls.append({"url": url, "headers": headers, "json": json})
        return self._responses.pop(0)


def _install_post_client(monkeypatch, responses: list[httpx.Response]) -> list[dict]:
    from connectors import openrouter_multimodal as orm

    calls: list[dict] = []
    monkeypatch.setattr(
        orm.httpx,
        "AsyncClient",
        lambda **_kwargs: _PostClient(responses, calls),
    )
    return calls


def _image_response(
    image_bytes: bytes = _PNG, media_type: str = "image/png"
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "created": 1,
            "data": [
                {
                    "b64_json": base64.b64encode(image_bytes).decode("ascii"),
                    "media_type": media_type,
                }
            ],
        },
    )


def _jpeg_response() -> httpx.Response:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color=(20, 60, 140)).save(output, format="JPEG")
    return _image_response(output.getvalue(), "image/jpeg")


@pytest.mark.asyncio
async def test_openrouter_image_generation_dedicated_endpoint_contract(monkeypatch):
    from connectors.openrouter_multimodal import generate_images

    calls = _install_post_client(monkeypatch, [_image_response(), _image_response()])
    images = await generate_images(
        model="openrouter/google/gemini-3.1-flash-image",
        prompt="A lighthouse",
        size="1024x1024",
        count=2,
        style="watercolor",
        api_key="or-secret-test-value",
        api_base="https://openrouter.ai/api/v1",
    )

    assert images == [_PNG, _PNG]
    assert len(calls) == 2  # the recommended endpoint advertises n=1
    for call in calls:
        assert call["url"] == "https://openrouter.ai/api/v1/images"
        assert call["headers"]["Authorization"] == "Bearer or-secret-test-value"
        assert call["json"] == {
            "model": "google/gemini-3.1-flash-image",
            "prompt": "A lighthouse\n\nVisual style: watercolor",
            "n": 1,
            "size": "1024x1024",
        }


@pytest.mark.asyncio
async def test_openrouter_image_generation_normalizes_live_jpeg_contract(monkeypatch):
    from connectors.openrouter_multimodal import generate_images

    _install_post_client(monkeypatch, [_jpeg_response()])
    images = await generate_images(
        model="openrouter/google/gemini-3.1-flash-image",
        prompt="A production probe",
        size="512x512",
        count=1,
        style=None,
        api_key="or-secret-test-value",
        api_base="https://openrouter.ai/api/v1",
    )

    assert len(images) == 1
    assert images[0].startswith(_PNG[:8])
    with Image.open(io.BytesIO(images[0])) as normalized:
        assert normalized.format == "PNG"
        assert normalized.size == (2, 2)


@pytest.mark.asyncio
async def test_openrouter_full_image_edit_uses_input_reference(monkeypatch):
    from connectors.openrouter_multimodal import edit_image

    calls = _install_post_client(monkeypatch, [_image_response()])
    images = await edit_image(
        model="openrouter/google/gemini-3.1-flash-image",
        image_bytes=_PNG,
        prompt="Make the sky coral",
        mask=None,
        operation="edit",
        api_key="or-secret-test-value",
        api_base="https://openrouter.ai/api/v1/",
    )

    assert images == [_PNG]
    payload = calls[0]["json"]
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/images"
    assert payload["model"] == "google/gemini-3.1-flash-image"
    assert payload["prompt"] == "Make the sky coral"
    assert payload["n"] == 1
    assert "mask" not in payload
    reference = payload["input_references"][0]
    assert reference["type"] == "image_url"
    assert reference["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_openrouter_mask_edit_fails_before_http(monkeypatch):
    from connectors.openrouter_multimodal import (
        UnsupportedOpenRouterSemantics,
        edit_image,
    )

    calls = _install_post_client(monkeypatch, [])
    with pytest.raises(UnsupportedOpenRouterSemantics, match="does not expose mask semantics"):
        await edit_image(
            model="openrouter/google/gemini-3.1-flash-image",
            image_bytes=_PNG,
            prompt="Only change the selected region",
            mask=_PNG,
            operation="edit",
            api_key="or-secret-test-value",
            api_base="https://openrouter.ai/api/v1",
        )
    assert calls == []


@pytest.mark.asyncio
async def test_image_connector_reports_openrouter_mask_limit_without_provider_call(monkeypatch):
    import uuid

    from connectors import image_gen
    from core.artifacts import read_artifact_content, save_artifact
    from core.config import Settings

    org_id = f"openrouter-mask-{uuid.uuid4()}"
    artifact_id = await save_artifact(
        _PNG,
        kind="image",
        title="mask contract source",
        org_id=org_id,
        mime_type="image/png",
        created_by="test",
    )
    monkeypatch.setattr(
        image_gen,
        "settings",
        Settings(
            image_model="openrouter/google/gemini-3.1-flash-image",
            openrouter_api_key="test-key",
        ),
    )
    provider_called = False

    async def should_not_call(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        return [_PNG]

    monkeypatch.setattr(image_gen, "_call_edit_provider", should_not_call)
    result = await image_gen.ImageGenConnector().execute(
        "image.edit",
        {
            "__org_id": org_id,
            "artifact_id": artifact_id,
            "prompt": "change only this region",
            "mask": base64.b64encode(_PNG).decode("ascii"),
        },
    )

    assert result.data == {
        "status": "error",
        "reason": "mask semantics are unsupported by the configured OpenRouter image endpoint",
    }
    assert "full-image reference edits" in result.summary
    assert provider_called is False
    assert await read_artifact_content(artifact_id) == _PNG


@pytest.mark.asyncio
async def test_openrouter_stt_json_base64_contract(monkeypatch):
    from connectors.openrouter_multimodal import transcribe_audio

    calls = _install_post_client(
        monkeypatch,
        [httpx.Response(200, json={"text": "  hello from audio  ", "usage": {}})],
    )
    wav = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"audio"
    text = await transcribe_audio(
        model="openrouter/openai/gpt-4o-mini-transcribe",
        audio_bytes=wav,
        mime="audio/wav",
        api_key="or-secret-test-value",
        api_base="https://openrouter.ai/api/v1",
    )

    assert text == "hello from audio"
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert calls[0]["json"] == {
        "model": "openai/gpt-4o-mini-transcribe",
        "input_audio": {
            "data": base64.b64encode(wav).decode("ascii"),
            "format": "wav",
        },
    }


@pytest.mark.asyncio
async def test_openrouter_tts_raw_mp3_and_default_voice_mapping(monkeypatch):
    from connectors.openrouter_multimodal import synthesize_speech

    calls = _install_post_client(
        monkeypatch,
        [httpx.Response(200, content=b"mp3-bytes", headers={"content-type": "audio/mpeg"})],
    )
    audio = await synthesize_speech(
        model="openrouter/x-ai/grok-voice-tts-1.0",
        text="Hello",
        voice="alloy",
        api_key="or-secret-test-value",
        api_base="https://openrouter.ai/api/v1",
    )

    assert audio == b"mp3-bytes"
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/audio/speech"
    assert calls[0]["headers"]["Accept"] == "audio/mpeg"
    assert calls[0]["json"] == {
        "model": "x-ai/grok-voice-tts-1.0",
        "input": "Hello",
        "voice": "Eve",
        "response_format": "mp3",
    }


@pytest.mark.asyncio
async def test_openrouter_errors_are_redacted(monkeypatch):
    from connectors.openrouter_multimodal import OpenRouterMultimodalError, transcribe_audio

    secret = "or-secret-must-not-appear"
    calls = _install_post_client(
        monkeypatch,
        [
            httpx.Response(
                401,
                content=json.dumps({"error": f"credential was {secret}"}).encode(),
                headers={"content-type": "application/json"},
            )
        ],
    )
    with pytest.raises(OpenRouterMultimodalError) as exc_info:
        await transcribe_audio(
            model="openrouter/openai/gpt-4o-mini-transcribe",
            audio_bytes=b"ID3audio",
            mime="audio/mpeg",
            api_key=secret,
            api_base="https://openrouter.ai/api/v1",
        )
    assert str(exc_info.value) == "OpenRouter request failed (auth_rejected)"
    assert secret not in str(exc_info.value)
    assert len(calls) == 1


def test_openrouter_base_url_must_be_credential_safe_https():
    from connectors.openrouter_multimodal import OpenRouterMultimodalError, openrouter_api_url

    assert (
        openrouter_api_url("https://openrouter.ai/api/v1/", "/images")
        == "https://openrouter.ai/api/v1/images"
    )
    for unsafe in (
        "http://openrouter.ai/api/v1",
        "https://user:password@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?forward=elsewhere",
    ):
        with pytest.raises(OpenRouterMultimodalError, match="credential-safe HTTPS"):
            openrouter_api_url(unsafe, "images")


@pytest.mark.asyncio
async def test_openrouter_connectors_are_unavailable_without_shared_key(monkeypatch):
    from connectors import image_gen, voice
    from core.config import Settings

    monkeypatch.setattr(
        image_gen,
        "settings",
        Settings(
            image_model="openrouter/google/gemini-3.1-flash-image",
            openrouter_api_key="",
        ),
    )
    monkeypatch.setattr(
        voice,
        "settings",
        Settings(
            stt_model="openrouter/openai/gpt-4o-mini-transcribe",
            tts_model="openrouter/x-ai/grok-voice-tts-1.0",
            openrouter_api_key="",
        ),
    )

    generated = await image_gen.ImageGenConnector().execute(
        "image.generate", {"prompt": "test"}
    )
    transcribed = await voice.VoiceConnector().execute(
        "voice.transcribe", {"audio_b64": base64.b64encode(b"ID3audio").decode("ascii")}
    )
    spoken = await voice.VoiceConnector().execute("voice.speak", {"text": "test"})

    assert generated.data["status"] == "unavailable"
    assert transcribed.data["status"] == "unavailable"
    assert spoken.data["status"] == "unavailable"
    assert "OPENROUTER_API_KEY is not configured" in generated.summary
    assert "OPENROUTER_API_KEY is not configured" in transcribed.summary
    assert "OPENROUTER_API_KEY is not configured" in spoken.summary


@pytest.mark.asyncio
async def test_non_openrouter_image_and_voice_models_stay_on_litellm(monkeypatch):
    import litellm
    from connectors import image_gen, voice
    from core.config import Settings

    image_calls: list[dict] = []
    stt_calls: list[dict] = []
    tts_calls: list[dict] = []

    class _ImageItem:
        b64_json = base64.b64encode(_PNG).decode("ascii")
        url = None

    class _ImageResponse:
        data = [_ImageItem()]

    async def fake_image(**kwargs):
        image_calls.append(kwargs)
        return _ImageResponse()

    async def fake_stt(**kwargs):
        stt_calls.append(kwargs)
        return type("Transcription", (), {"text": "litellm transcript"})()

    async def fake_tts(**kwargs):
        tts_calls.append(kwargs)
        return type("Speech", (), {"content": b"litellm audio"})()

    monkeypatch.setattr(litellm, "aimage_generation", fake_image)
    monkeypatch.setattr(litellm, "atranscription", fake_stt)
    monkeypatch.setattr(litellm, "aspeech", fake_tts)
    monkeypatch.setattr(image_gen, "settings", Settings(image_model="vertex_ai/imagen-3"))
    monkeypatch.setattr(
        voice,
        "settings",
        Settings(stt_model="openai/whisper-1", tts_model="openai/tts-1"),
    )

    assert await image_gen._call_provider("prompt", "1024x1024", 1) == [_PNG]
    assert await voice._call_stt(b"ID3audio", "audio/mpeg") == "litellm transcript"
    assert await voice._call_tts("hello", "alloy") == b"litellm audio"
    assert image_calls[0]["model"] == "vertex_ai/imagen-3"
    assert stt_calls[0]["model"] == "openai/whisper-1"
    assert tts_calls[0]["model"] == "openai/tts-1"


@pytest.mark.asyncio
async def test_openrouter_health_verifies_models_without_generation(monkeypatch):
    from core import connector_health

    checked_at = connector_health._utcnow()

    async def accepted():
        return connector_health.ProbeResult(True, checked_at, 7)

    async def no_playwright():
        return False, "not installed"

    monkeypatch.setattr(connector_health.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(
        connector_health.settings,
        "vision_model",
        "openrouter/openai/gpt-4o-mini",
    )
    monkeypatch.setattr(
        connector_health.settings,
        "image_model",
        "openrouter/google/gemini-3.1-flash-image",
    )
    monkeypatch.setattr(
        connector_health.settings,
        "stt_model",
        "openrouter/openai/gpt-4o-mini-transcribe",
    )
    monkeypatch.setattr(
        connector_health.settings,
        "tts_model",
        "openrouter/x-ai/grok-voice-tts-1.0",
    )
    monkeypatch.setattr(connector_health.settings, "browserbase_api_key", "")
    monkeypatch.setattr(connector_health.settings, "e2b_api_key", "")
    monkeypatch.setattr(connector_health.settings, "composio_api_key", "")
    monkeypatch.setattr(connector_health, "_probe_openrouter", accepted)
    monkeypatch.setattr(connector_health, "_playwright_available", no_playwright)
    monkeypatch.setattr(connector_health, "_CACHE", None)
    monkeypatch.setattr(connector_health, "_LAST_VERIFIED", {})

    health = await connector_health.check_connectors(refresh=True)

    assert health["openrouter"]["status"] == "verified"
    assert health["image"]["tier"] == "live"
    assert health["image"]["model"] == "openrouter/google/gemini-3.1-flash-image"
    assert health["voice"]["status"] == "live"
    assert health["stt"]["verified"] is True
    assert health["tts"]["verified"] is True


@pytest.mark.asyncio
async def test_openrouter_model_health_is_truthful_without_key(monkeypatch):
    from core import connector_health

    async def no_playwright():
        return False, "not installed"

    monkeypatch.setattr(connector_health.settings, "openrouter_api_key", "")
    monkeypatch.setattr(
        connector_health.settings,
        "image_model",
        "openrouter/google/gemini-3.1-flash-image",
    )
    monkeypatch.setattr(connector_health.settings, "browserbase_api_key", "")
    monkeypatch.setattr(connector_health.settings, "e2b_api_key", "")
    monkeypatch.setattr(connector_health.settings, "composio_api_key", "")
    monkeypatch.setattr(connector_health, "_playwright_available", no_playwright)
    monkeypatch.setattr(connector_health, "_CACHE", None)
    monkeypatch.setattr(connector_health, "_LAST_VERIFIED", {})

    health = await connector_health.check_connectors(refresh=True)

    assert health["image"]["status"] == "unavailable"
    assert health["image"]["configured"] is False
    assert "OPENROUTER_API_KEY is not configured" in health["image"]["reason"]
