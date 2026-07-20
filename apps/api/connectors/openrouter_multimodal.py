"""Direct OpenRouter multimodal HTTP contracts.

LiteLLM remains the provider abstraction for non-OpenRouter model identifiers.
OpenRouter's dedicated Images, Transcriptions, and Speech endpoints have
provider-specific wire formats that are small and stable enough to implement
directly.  This module deliberately keeps provider error bodies out of raised
exceptions because they are not part of Chronos' public error contract and may
contain request details.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
from typing import Any
from urllib.parse import urlsplit

import httpx


class OpenRouterMultimodalError(RuntimeError):
    """A redacted OpenRouter transport or response-contract failure."""


class UnsupportedOpenRouterSemantics(OpenRouterMultimodalError):
    """The requested operation cannot be represented by OpenRouter's endpoint."""


_OPENROUTER_PREFIX = "openrouter/"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_IMAGE_RESPONSE_BYTES = 25 * 1024 * 1024
_MAX_IMAGE_PIXELS = 4096 * 4096
_MAX_STT_BYTES = 25 * 1024 * 1024
_GROK_TTS_MODEL = "x-ai/grok-voice-tts-1.0"
_GROK_VOICES = {
    "eve": "Eve",
    "ara": "Ara",
    "rex": "Rex",
    "sal": "Sal",
    "leo": "Leo",
}
_OPENAI_TO_GROK_VOICE = {
    "alloy": "Eve",
    "ash": "Rex",
    "ballad": "Ara",
    "coral": "Sal",
    "echo": "Rex",
    "fable": "Ara",
    "nova": "Sal",
    "onyx": "Leo",
    "sage": "Leo",
    "shimmer": "Eve",
    "verse": "Ara",
}


def is_openrouter_model(model: str) -> bool:
    """Return whether *model* opts into the direct OpenRouter path."""

    return model.strip().lower().startswith(_OPENROUTER_PREFIX)


def openrouter_model_slug(model: str) -> str:
    """Strip Chronos' ``openrouter/`` routing prefix for OpenRouter's payload."""

    value = model.strip()
    if not is_openrouter_model(value):
        raise ValueError("model is not prefixed with openrouter/")
    slug = value[len(_OPENROUTER_PREFIX) :].strip()
    if not slug or "/" not in slug:
        raise OpenRouterMultimodalError("OpenRouter model identifier is invalid")
    return slug


def resolve_openrouter_tts_voice(model: str, requested_voice: str) -> str:
    """Resolve Chronos' default/common voices for the configured TTS model.

    The production default is xAI Grok Voice TTS, whose documented built-in
    voices differ from the OpenAI-compatible names Chronos historically exposed.
    Common names are mapped deterministically; unknown names fail rather than
    silently producing a different voice.
    """

    slug = openrouter_model_slug(model)
    voice = requested_voice.strip()
    if not voice:
        voice = "alloy"
    if slug != _GROK_TTS_MODEL:
        return voice

    normalized = voice.lower()
    if normalized in _GROK_VOICES:
        return _GROK_VOICES[normalized]
    if normalized in _OPENAI_TO_GROK_VOICE:
        return _OPENAI_TO_GROK_VOICE[normalized]
    raise UnsupportedOpenRouterSemantics(
        "The configured Grok Voice TTS model accepts Eve, Ara, Rex, Sal, or Leo; "
        "the requested voice has no safe mapping"
    )


def openrouter_api_url(api_base: str, path: str) -> str:
    """Build a credential-safe OpenRouter endpoint URL.

    Custom OpenRouter-compatible gateways remain possible, but credentials are
    never sent over plaintext HTTP or embedded-authority URLs.
    """

    base = api_base.strip().rstrip("/")
    if not base:
        raise OpenRouterMultimodalError("OPENROUTER_API_BASE is not configured")
    parsed = urlsplit(base)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise OpenRouterMultimodalError(
            "OPENROUTER_API_BASE must be a credential-safe HTTPS base URL"
        )
    return f"{base}/{path.lstrip('/')}"


def _headers(api_key: str) -> dict[str, str]:
    key = api_key.strip()
    if not key:
        raise OpenRouterMultimodalError("OPENROUTER_API_KEY is not configured")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _redacted_status(status_code: int) -> str:
    return {
        400: "invalid_request",
        401: "auth_rejected",
        402: "payment_required",
        403: "permission_denied",
        404: "endpoint_or_model_not_found",
        408: "timeout",
        413: "payload_too_large",
        422: "invalid_request",
        429: "rate_limited",
        500: "provider_error",
        502: "provider_unavailable",
        503: "provider_unavailable",
        504: "timeout",
    }.get(status_code, "unexpected_response")


async def _post(
    *,
    api_base: str,
    api_key: str,
    path: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    accept: str,
) -> httpx.Response:
    headers = _headers(api_key)
    headers["Accept"] = accept
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        ) as client:
            response = await client.post(
                openrouter_api_url(api_base, path),
                headers=headers,
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise OpenRouterMultimodalError("OpenRouter request timed out") from exc
    except httpx.RequestError as exc:
        raise OpenRouterMultimodalError("OpenRouter request failed at the network boundary") from exc
    except OpenRouterMultimodalError:
        raise
    except Exception as exc:  # noqa: BLE001 - keep client exception details redacted
        raise OpenRouterMultimodalError("OpenRouter request could not be completed") from exc

    if response.status_code < 200 or response.status_code >= 300:
        code = _redacted_status(response.status_code)
        raise OpenRouterMultimodalError(f"OpenRouter request failed ({code})")
    return response


def _json_object(response: httpx.Response, *, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - provider payload is intentionally not surfaced
        raise OpenRouterMultimodalError(
            f"OpenRouter {operation} returned malformed JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise OpenRouterMultimodalError(
            f"OpenRouter {operation} returned an unexpected response"
        )
    return payload


def _decode_png_images(payload: dict[str, Any], *, operation: str) -> list[bytes]:
    """Validate provider images and normalize supported formats to PNG.

    OpenRouter's Images endpoint may return JPEG even when the requested model
    historically returned PNG. Chronos stores generated images under a stable
    PNG artifact contract, so provider bytes are decoded with strict byte/pixel
    limits and non-PNG formats are flattened into a single safe PNG frame.
    """

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise OpenRouterMultimodalError(f"OpenRouter {operation} returned no images")

    images: list[bytes] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("b64_json"), str):
            raise OpenRouterMultimodalError(
                f"OpenRouter {operation} returned an unexpected image item"
            )
        try:
            decoded = base64.b64decode(item["b64_json"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise OpenRouterMultimodalError(
                f"OpenRouter {operation} returned invalid base64 image data"
            ) from exc
        if not decoded or len(decoded) > _MAX_IMAGE_RESPONSE_BYTES:
            raise OpenRouterMultimodalError(
                f"OpenRouter {operation} returned an oversized image"
            )
        media_type = str(item.get("media_type") or "").lower()
        declared_formats = {
            "image/png": "PNG",
            "image/jpeg": "JPEG",
            "image/jpg": "JPEG",
            "image/webp": "WEBP",
            "image/gif": "GIF",
        }
        if media_type and media_type not in declared_formats:
            raise OpenRouterMultimodalError(
                f"OpenRouter {operation} returned an unsupported image format"
            )

        try:
            from PIL import Image, UnidentifiedImageError

            with Image.open(io.BytesIO(decoded)) as source:
                detected_format = str(source.format or "").upper()
                if detected_format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                    raise OpenRouterMultimodalError(
                        f"OpenRouter {operation} returned an unsupported image format"
                    )
                if media_type and declared_formats[media_type] != detected_format:
                    raise OpenRouterMultimodalError(
                        f"OpenRouter {operation} returned mismatched image metadata"
                    )
                width, height = source.size
                if (
                    width < 1
                    or height < 1
                    or width * height > _MAX_IMAGE_PIXELS
                ):
                    raise OpenRouterMultimodalError(
                        f"OpenRouter {operation} returned an oversized image"
                    )
                source.seek(0)
                source.load()
                if detected_format == "PNG" and decoded.startswith(_PNG_SIGNATURE):
                    images.append(decoded)
                    continue
                frame = source.convert(
                    "RGBA"
                    if source.mode in {"RGBA", "LA"} or "transparency" in source.info
                    else "RGB"
                )
                normalized = io.BytesIO()
                frame.save(normalized, format="PNG")
                normalized_bytes = normalized.getvalue()
        except OpenRouterMultimodalError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise OpenRouterMultimodalError(
                f"OpenRouter {operation} returned invalid image data"
            ) from exc
        images.append(normalized_bytes)
    return images


def _validated_size(size: str) -> str:
    value = size.strip()
    if value in {"512", "1K", "2K", "4K"}:
        return value
    match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", value)
    if not match:
        raise OpenRouterMultimodalError("Image size is not a supported size value")
    width, height = (int(match.group(1)), int(match.group(2)))
    if not (256 <= width <= 8192 and 256 <= height <= 8192):
        raise OpenRouterMultimodalError("Image dimensions must be between 256 and 8192 pixels")
    return value


def _input_image_data_url(image_bytes: bytes) -> str:
    if image_bytes.startswith(_PNG_SIGNATURE):
        media_type = "image/png"
    elif image_bytes.startswith(b"\xff\xd8\xff"):
        media_type = "image/jpeg"
    elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        media_type = "image/webp"
    elif image_bytes.startswith((b"GIF87a", b"GIF89a")):
        media_type = "image/gif"
    else:
        raise OpenRouterMultimodalError("Source image format could not be identified")
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


async def generate_images(
    *,
    model: str,
    prompt: str,
    size: str,
    count: int,
    style: str | None,
    api_key: str,
    api_base: str,
) -> list[bytes]:
    """Generate PNGs with OpenRouter's dedicated Images API.

    The recommended Gemini image endpoint currently supports one image per
    request. Chronos preserves its 1-4 count contract by issuing bounded,
    sequential one-image requests rather than sending an unsupported ``n``.
    """

    if count < 1 or count > 4:
        raise OpenRouterMultimodalError("Image count must be between 1 and 4")
    effective_prompt = prompt.strip()
    if style and style.strip():
        effective_prompt = f"{effective_prompt}\n\nVisual style: {style.strip()}"
    request = {
        "model": openrouter_model_slug(model),
        "prompt": effective_prompt,
        "n": 1,
        "size": _validated_size(size),
    }
    generated: list[bytes] = []
    for _ in range(count):
        response = await _post(
            api_base=api_base,
            api_key=api_key,
            path="images",
            payload=request,
            timeout_seconds=180.0,
            accept="application/json",
        )
        generated.extend(
            _decode_png_images(_json_object(response, operation="image generation"), operation="image generation")
        )
    return generated


async def edit_image(
    *,
    model: str,
    image_bytes: bytes,
    prompt: str,
    mask: bytes | None,
    operation: str,
    api_key: str,
    api_base: str,
) -> list[bytes]:
    """Perform a full-image reference edit through OpenRouter's Images API."""

    if mask is not None:
        raise UnsupportedOpenRouterSemantics(
            "OpenRouter's dedicated Images API does not expose mask semantics; no edit was attempted"
        )
    operation_prompt = prompt.strip()
    if operation and operation != "edit":
        operation_prompt = f"{operation.capitalize()} operation: {operation_prompt}"
    request = {
        "model": openrouter_model_slug(model),
        "prompt": operation_prompt,
        "n": 1,
        "input_references": [
            {
                "type": "image_url",
                "image_url": {"url": _input_image_data_url(image_bytes)},
            }
        ],
    }
    response = await _post(
        api_base=api_base,
        api_key=api_key,
        path="images",
        payload=request,
        timeout_seconds=180.0,
        accept="application/json",
    )
    return _decode_png_images(
        _json_object(response, operation="image editing"), operation="image editing"
    )


def _audio_format(mime: str, audio_bytes: bytes) -> str:
    normalized = mime.lower().split(";", 1)[0].strip()
    by_mime = {
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/webm": "webm",
        "audio/ogg": "ogg",
        "audio/mp4": "m4a",
        "audio/m4a": "m4a",
        "audio/x-m4a": "m4a",
        "audio/flac": "flac",
    }
    if normalized in by_mime:
        return by_mime[normalized]
    if audio_bytes.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        return "mp3"
    if audio_bytes.startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE":
        return "wav"
    if audio_bytes.startswith(b"OggS"):
        return "ogg"
    if audio_bytes.startswith(b"fLaC"):
        return "flac"
    if audio_bytes.startswith(b"\x1aE\xdf\xa3"):
        return "webm"
    raise OpenRouterMultimodalError("Audio format could not be identified for transcription")


async def transcribe_audio(
    *,
    model: str,
    audio_bytes: bytes,
    mime: str,
    api_key: str,
    api_base: str,
) -> str:
    """Transcribe JSON/base64 audio through OpenRouter's dedicated endpoint."""

    if not audio_bytes:
        raise OpenRouterMultimodalError("Audio input is empty")
    if len(audio_bytes) > _MAX_STT_BYTES:
        raise OpenRouterMultimodalError("Audio input exceeds the 25 MiB transcription limit")
    request = {
        "model": openrouter_model_slug(model),
        "input_audio": {
            "data": base64.b64encode(audio_bytes).decode("ascii"),
            "format": _audio_format(mime, audio_bytes),
        },
    }
    response = await _post(
        api_base=api_base,
        api_key=api_key,
        path="audio/transcriptions",
        payload=request,
        timeout_seconds=180.0,
        accept="application/json",
    )
    payload = _json_object(response, operation="transcription")
    transcript = payload.get("text")
    if not isinstance(transcript, str) or not transcript.strip():
        raise OpenRouterMultimodalError("OpenRouter transcription returned no text")
    return transcript.strip()


async def synthesize_speech(
    *,
    model: str,
    text: str,
    voice: str,
    api_key: str,
    api_base: str,
) -> bytes:
    """Synthesize MP3 audio through OpenRouter's dedicated Speech endpoint."""

    slug = openrouter_model_slug(model)
    if slug == _GROK_TTS_MODEL and len(text) > 15_000:
        raise OpenRouterMultimodalError("Grok Voice TTS input exceeds 15,000 characters")
    request = {
        "model": slug,
        "input": text,
        "voice": resolve_openrouter_tts_voice(model, voice),
        "response_format": "mp3",
    }
    response = await _post(
        api_base=api_base,
        api_key=api_key,
        path="audio/speech",
        payload=request,
        timeout_seconds=180.0,
        accept="audio/mpeg",
    )
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"audio/mpeg", "audio/mp3"}:
        raise OpenRouterMultimodalError("OpenRouter speech returned an unexpected content type")
    if not response.content:
        raise OpenRouterMultimodalError("OpenRouter speech returned empty audio")
    return bytes(response.content)
