from __future__ import annotations

"""Voice connector — speech-to-text (STT) and text-to-speech (TTS).

Routes through tool_broker.execute only. Two tools:

- ``voice.transcribe``: converts audio bytes (from an uploaded artifact or
  inline base64) to text and persists a ``kind="transcript"`` artifact.
- ``voice.speak``: converts text to audio bytes and persists a ``kind="audio"``
  artifact (audio/mpeg).

Provider abstraction: both provider calls go through single stubbable seams
``_call_stt`` and ``_call_tts``. Tests monkeypatch those functions. The
connector saves artifacts and returns ids + metadata — no binary blobs in
ToolResult.data.

Honest degraded leaf: when ``settings.stt_model`` or ``settings.tts_model`` is
empty the connector returns status "unavailable" with a ``fallback_reason``
rather than raising. Provider exceptions are caught and returned as honest
"error" ToolResults — never propagated into the broker/SSE stream.
"""

import base64
import re
from typing import Any

from core.config import settings
from core.models import ToolResult

#: UUID heuristic — distinguishes artifact id from inline base64 audio data.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_DEFAULT_VOICE = "alloy"
_TEXT_PREVIEW_LEN = 120


# ── Provider seams (stubbable in tests) ──────────────────────────────────────


async def _call_stt(audio_bytes: bytes, mime: str) -> str:
    """Call the configured STT provider and return the transcript string.

    This function is the single stubbable seam for STT provider I/O. In tests,
    monkeypatch ``connectors.voice._call_stt`` to return deterministic text
    without making real network calls.

    Args:
        audio_bytes: Raw audio bytes (mp3, wav, webm, ogg, m4a, etc.).
        mime: MIME type of the audio (e.g. "audio/mpeg", "audio/webm").

    Returns:
        Transcript string from the provider.

    Raises:
        RuntimeError: When the provider returns an empty or unexpected response.
    """
    from connectors.openrouter_multimodal import is_openrouter_model, transcribe_audio

    if is_openrouter_model(settings.stt_model):
        return await transcribe_audio(
            model=settings.stt_model,
            audio_bytes=audio_bytes,
            mime=mime,
            api_key=settings.openrouter_api_key,
            api_base=settings.openrouter_api_base,
        )

    import io
    import litellm  # type: ignore[import]

    # litellm's transcription endpoint mirrors the OpenAI Audio Transcriptions API.
    # We pass an in-memory file-like object with a sensible name based on mime type.
    ext_map = {
        "audio/mpeg": "audio.mp3",
        "audio/mp3": "audio.mp3",
        "audio/wav": "audio.wav",
        "audio/x-wav": "audio.wav",
        "audio/webm": "audio.webm",
        "audio/ogg": "audio.ogg",
        "audio/m4a": "audio.m4a",
        "audio/mp4": "audio.m4a",
        "audio/flac": "audio.flac",
    }
    filename = ext_map.get(mime.lower().split(";")[0].strip(), "audio.mp3")
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = filename  # type: ignore[attr-defined]

    response = await litellm.atranscription(
        model=settings.stt_model,
        file=audio_file,
    )
    text: str = getattr(response, "text", None) or ""
    if not text:
        raise RuntimeError("STT provider returned an empty transcript")
    return text


async def _call_tts(text: str, voice: str) -> bytes:
    """Call the configured TTS provider and return raw audio bytes.

    This function is the single stubbable seam for TTS provider I/O. In tests,
    monkeypatch ``connectors.voice._call_tts`` to return deterministic bytes
    without making real network calls.

    Args:
        text: The text to synthesise.
        voice: Provider voice identifier (e.g. "alloy", "echo", "fable").

    Returns:
        Raw audio bytes (typically audio/mpeg).

    Raises:
        RuntimeError: When the provider returns empty content.
    """
    from connectors.openrouter_multimodal import is_openrouter_model, synthesize_speech

    if is_openrouter_model(settings.tts_model):
        return await synthesize_speech(
            model=settings.tts_model,
            text=text,
            voice=voice,
            api_key=settings.openrouter_api_key,
            api_base=settings.openrouter_api_base,
        )

    import litellm  # type: ignore[import]

    response = await litellm.aspeech(
        model=settings.tts_model,
        input=text,
        voice=voice,
    )
    # litellm returns an httpx.Response-like object; .content holds the bytes.
    audio_bytes: bytes = getattr(response, "content", b"") or b""
    if not audio_bytes:
        raise RuntimeError("TTS provider returned empty audio content")
    return audio_bytes


# ── Connector ────────────────────────────────────────────────────────────────


class VoiceConnector:
    """Connector for ``voice.transcribe`` (STT) and ``voice.speak`` (TTS)."""

    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Execute a voice tool call (transcribe or speak).

        Args:
            tool: "voice.transcribe" or "voice.speak".
            args: Tool arguments including broker-injected keys
                ``__connector_tier``, ``__org_id``, ``__task_id``, and
                ``__member_id``.

        Returns:
            ToolResult with data containing artifact id(s) and metadata, and an
            honest status. When no provider is configured, returns an
            "unavailable" ToolResult rather than raising.
        """
        args.pop("__connector_tier", None)
        org_id: str = str(args.pop("__org_id", "default") or "default")
        task_id: str | None = args.pop("__task_id", None)
        member_id = str(args.pop("__member_id", "voice_connector") or "voice_connector")

        if tool == "voice.transcribe":
            return await self._execute_transcribe(
                args,
                org_id=org_id,
                task_id=task_id,
                member_id=member_id,
            )
        elif tool == "voice.speak":
            return await self._execute_speak(
                args,
                org_id=org_id,
                task_id=task_id,
                member_id=member_id,
            )
        else:
            raise ValueError(f"Unknown voice tool: {tool!r}")

    # ── voice.transcribe ────────────────────────────────────────────────────

    async def _execute_transcribe(
        self,
        args: dict[str, Any],
        *,
        org_id: str,
        task_id: str | None,
        member_id: str = "voice_connector",
    ) -> ToolResult:
        """Handle ``voice.transcribe`` (speech-to-text).

        Accepts either an ``artifact_id`` pointing to an uploaded audio artifact
        or an ``audio_b64`` inline base64 string. Resolves and org-checks the
        artifact (cross-org access is rejected with an honest error). Calls the
        STT provider seam, saves the transcript as a ``kind="transcript"``
        artifact (text/plain), and returns the transcript text plus the
        transcript artifact id.

        Args:
            args: Tool arguments (artifact_id OR audio_b64, optional mime_type,
                optional conversation_id).
            org_id: Tenant scope injected by the broker.
            task_id: Current task id injected by the broker (may be None).

        Returns:
            ToolResult with transcript text and transcript artifact id, or an
            honest "unavailable"/"error" result.
        """
        # Honest degraded leaf: no STT provider configured.
        if not settings.stt_model:
            return ToolResult(
                data={
                    "status": "unavailable",
                    "fallback_reason": "no STT provider configured",
                },
                summary=(
                    "Speech-to-text is not available: no STT provider is configured. "
                    "Set STT_MODEL in your environment to enable transcription."
                ),
            )
        from connectors.openrouter_multimodal import is_openrouter_model

        if is_openrouter_model(settings.stt_model) and not settings.openrouter_api_key.strip():
            return ToolResult(
                data={
                    "status": "unavailable",
                    "fallback_reason": "OpenRouter STT credentials are not configured",
                },
                summary=(
                    "Speech-to-text is not available: STT_MODEL selects OpenRouter, "
                    "but OPENROUTER_API_KEY is not configured."
                ),
            )

        artifact_id: str | None = str(args.get("artifact_id") or "").strip() or None
        audio_b64: str | None = args.get("audio_b64") or None
        conversation_id: str | None = args.get("conversation_id") or None

        if not artifact_id and not audio_b64:
            return ToolResult(
                data={"status": "error", "reason": "artifact_id or audio_b64 is required"},
                summary="voice.transcribe: artifact_id or audio_b64 is required",
            )

        # Resolve audio bytes — either from an artifact or from inline base64.
        audio_bytes: bytes
        mime: str = str(args.get("mime_type") or "audio/mpeg")

        if artifact_id:
            from core.artifacts import get_artifact, read_artifact_content

            meta = await get_artifact(artifact_id)
            if meta is None or meta.get("is_deleted"):
                return ToolResult(
                    data={"status": "error", "reason": "audio artifact not found"},
                    summary=f"voice.transcribe: artifact {artifact_id!r} not found",
                )
            if str(meta.get("organization_id", "")) != str(org_id):
                return ToolResult(
                    data={"status": "error", "reason": "audio artifact not found"},
                    summary=f"voice.transcribe: artifact {artifact_id!r} not found in this organization",
                )
            raw = await read_artifact_content(artifact_id)
            if not raw:
                return ToolResult(
                    data={"status": "error", "reason": "audio artifact content could not be read"},
                    summary=f"voice.transcribe: could not read content for artifact {artifact_id!r}",
                )
            audio_bytes = raw
            mime = str(meta.get("mime_type") or mime)
        else:
            # Inline base64 path.
            assert audio_b64 is not None
            try:
                audio_bytes = base64.b64decode(audio_b64)
            except Exception:
                return ToolResult(
                    data={"status": "error", "reason": "audio_b64 could not be base64-decoded"},
                    summary="voice.transcribe: audio_b64 is not valid base64",
                )

        # Call the STT provider (stubbed in tests via monkeypatch). Any provider or
        # network error must degrade honestly — never propagate into the broker stream.
        try:
            transcript_text = await _call_stt(audio_bytes, mime)
        except Exception as exc:
            return ToolResult(
                data={"status": "error", "reason": f"STT provider call failed: {type(exc).__name__}"},
                summary=f"Transcription failed: {exc}",
            )

        if not transcript_text:
            return ToolResult(
                data={"status": "error", "reason": "STT provider returned an empty transcript"},
                summary="Transcription produced no text (provider returned an empty result).",
            )

        # Persist the transcript as an artifact.
        from core.artifacts import save_artifact

        preview = transcript_text[:80].replace("\n", " ")
        title = f"Transcript: {preview}{'…' if len(transcript_text) > 80 else ''}"
        transcript_artifact_id = await save_artifact(
            transcript_text.encode("utf-8"),
            kind="transcript",
            title=title,
            conversation_id=conversation_id,
            task_id=task_id,
            org_id=org_id,
            mime_type="text/plain",
            created_by=member_id,
        )

        return ToolResult(
            data={
                "status": "success",
                "transcript": transcript_text,
                "transcript_artifact_id": transcript_artifact_id,
                "model": settings.stt_model,
                "source_artifact_id": artifact_id,
            },
            summary=f"Transcribed audio: {preview}{'…' if len(transcript_text) > 80 else ''}",
        )

    # ── voice.speak ─────────────────────────────────────────────────────────

    async def _execute_speak(
        self,
        args: dict[str, Any],
        *,
        org_id: str,
        task_id: str | None,
        member_id: str = "voice_connector",
    ) -> ToolResult:
        """Handle ``voice.speak`` (text-to-speech).

        Converts the provided text to audio via the TTS provider seam. Saves the
        audio as a ``kind="audio"`` artifact (audio/mpeg) with TTS metadata baked
        into the title (text preview, voice, model). Returns the audio artifact id
        and synthesis metadata.

        Args:
            args: Tool arguments (text required, optional voice, optional
                conversation_id).
            org_id: Tenant scope injected by the broker.
            task_id: Current task id injected by the broker (may be None).

        Returns:
            ToolResult with audio artifact id and TTS metadata, or an honest
            "unavailable"/"error" result.
        """
        text: str = str(args.get("text") or "").strip()
        if not text:
            return ToolResult(
                data={"status": "error", "reason": "text is required"},
                summary="voice.speak: text is required",
            )

        voice: str = str(args.get("voice") or _DEFAULT_VOICE)
        conversation_id: str | None = args.get("conversation_id") or None

        # Honest degraded leaf: no TTS provider configured.
        if not settings.tts_model:
            return ToolResult(
                data={
                    "status": "unavailable",
                    "fallback_reason": "no TTS provider configured",
                },
                summary=(
                    "Text-to-speech is not available: no TTS provider is configured. "
                    "Set TTS_MODEL in your environment to enable speech synthesis."
                ),
            )
        from connectors.openrouter_multimodal import is_openrouter_model

        if is_openrouter_model(settings.tts_model) and not settings.openrouter_api_key.strip():
            return ToolResult(
                data={
                    "status": "unavailable",
                    "fallback_reason": "OpenRouter TTS credentials are not configured",
                },
                summary=(
                    "Text-to-speech is not available: TTS_MODEL selects OpenRouter, "
                    "but OPENROUTER_API_KEY is not configured."
                ),
            )

        # Call the TTS provider (stubbed in tests via monkeypatch). Any provider or
        # network error must degrade honestly — never propagate into the broker stream.
        try:
            audio_bytes = await _call_tts(text, voice)
        except Exception as exc:
            return ToolResult(
                data={"status": "error", "reason": f"TTS provider call failed: {type(exc).__name__}"},
                summary=f"Speech synthesis failed: {exc}",
            )

        if not audio_bytes:
            return ToolResult(
                data={"status": "error", "reason": "TTS provider returned empty audio"},
                summary="Speech synthesis produced no audio (provider returned an empty result).",
            )

        # Build a title that carries the key TTS metadata for reopenable persistence.
        # save_artifact has no metadata param — bake into title (mirrors image_gen).
        model_name: str = settings.tts_model
        text_preview = text[:_TEXT_PREVIEW_LEN].replace("\n", " ")
        title = (
            f"TTS [{voice}/{model_name}]: "
            f"{text_preview}{'…' if len(text) > _TEXT_PREVIEW_LEN else ''}"
        )

        from core.artifacts import save_artifact

        audio_artifact_id = await save_artifact(
            audio_bytes,
            kind="audio",
            title=title,
            conversation_id=conversation_id,
            task_id=task_id,
            org_id=org_id,
            mime_type="audio/mpeg",
            created_by=member_id,
        )

        tts_meta: dict[str, Any] = {
            "text_preview": text_preview,
            "text_length": len(text),
            "voice": voice,
            "model": model_name,
        }
        from connectors.openrouter_multimodal import resolve_openrouter_tts_voice

        if is_openrouter_model(model_name):
            provider_voice = resolve_openrouter_tts_voice(model_name, voice)
            if provider_voice != voice:
                tts_meta["provider_voice"] = provider_voice

        return ToolResult(
            data={
                "status": "success",
                "audio_artifact_id": audio_artifact_id,
                "tts_meta": tts_meta,
            },
            summary=f"Synthesised speech for: {text_preview}{'…' if len(text) > _TEXT_PREVIEW_LEN else ''}",
        )


voice_connector = VoiceConnector()
