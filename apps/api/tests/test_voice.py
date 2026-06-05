"""Acceptance proof for Phase 7 Task 6: Voice input/output (STT + TTS).

Tests verify:
- voice.transcribe routes through the tool broker: saves a kind="transcript"
  artifact, returns transcript text + artifact id, audit trail present.
- voice.speak routes through the tool broker: saves a kind="audio" artifact
  with TTS metadata in the title, returns audio artifact id, audit trail present.
- Both artifacts are reopenable via get_artifact / read_artifact_content after
  the broker call (real DB persistence round-trip).
- Honest degraded: stt_model="" → status "unavailable", no artifact, audited.
- Honest degraded: tts_model="" → status "unavailable", no artifact, audited.
- Provider error: stubbed _call_stt raises → honest error ToolResult, no artifact,
  no propagation.
- Provider error: stubbed _call_tts raises → honest error ToolResult, no artifact,
  no propagation.
- Cross-org: org-B agent transcribing org-A audio artifact → rejected with honest
  error, no transcript artifact created, exercising the real connector org check.
- Tool registry: VOICE_TRANSCRIBE / VOICE_SPEAK appear in ALL_TOOLS, SUBAGENT_TOOLS,
  INLINE_CHAT_TOOLS.
- Broker name conversion: voice__transcribe → voice.transcribe.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

# ---------------------------------------------------------------------------
# Helpers (mirrors test_image_generation.py pattern)
# ---------------------------------------------------------------------------

_ORG_COUNTER = iter(range(3000))


def _unique_org() -> str:
    return f"voice-org-{next(_ORG_COUNTER)}"


def _make_agent(org_id: str = "default"):
    import uuid
    from core.models import AgentContext

    return AgentContext(
        id=str(uuid.uuid4()),
        org_id=org_id,
        task_id=str(uuid.uuid4()),
        member_id=str(uuid.uuid4()),
    )


# Minimal valid MP3 header bytes (enough to round-trip; not a real playable file).
_FAKE_AUDIO = bytes([
    0xFF, 0xFB, 0x90, 0x00,  # MPEG frame sync + header bytes
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])

_FAKE_TRANSCRIPT = "Hello, this is a test transcript."


def _patch_broker_infra(monkeypatch):
    """Patch broker audit, permissions, tool_policy, connector_tier.

    Returns audited event list for assertions. Unique org ids keep tests under
    the 10-actions/minute/org rate cap without patching rate limiting itself.
    """
    from core import tool_broker as tb

    audited: list[str] = []

    async def fake_log(event_type, actor, action, **kw):
        audited.append(event_type)

    async def fake_check(*a, **k):
        return True

    async def fake_tool_policy(*a, **k):
        return {}

    monkeypatch.setattr(tb.audit, "log", fake_log)
    monkeypatch.setattr(tb.permissions, "check", fake_check)
    monkeypatch.setattr(tb, "tool_policy", fake_tool_policy)
    monkeypatch.setattr(tb, "connector_tier", AsyncMock(return_value="live"))
    return audited


def _force_voice_models(monkeypatch, stt: str = "test/whisper-stub", tts: str = "test/tts-stub") -> None:
    """Override settings.stt_model and settings.tts_model in the connector module."""
    import connectors.voice as vc
    from core.config import Settings

    fake_settings = Settings(stt_model=stt, tts_model=tts)
    monkeypatch.setattr(vc, "settings", fake_settings)


def _stub_stt(monkeypatch, transcript: str = _FAKE_TRANSCRIPT) -> None:
    """Monkeypatch _call_stt to return deterministic transcript."""
    import connectors.voice as vc

    async def fake_call_stt(audio_bytes: bytes, mime: str) -> str:
        return transcript

    monkeypatch.setattr(vc, "_call_stt", fake_call_stt)


def _stub_tts(monkeypatch, audio: bytes = _FAKE_AUDIO) -> None:
    """Monkeypatch _call_tts to return deterministic audio bytes."""
    import connectors.voice as vc

    async def fake_call_tts(text: str, voice: str) -> bytes:
        return audio

    monkeypatch.setattr(vc, "_call_tts", fake_call_tts)


# ---------------------------------------------------------------------------
# Test 1: STT success — artifact created, transcript returned, audited, readable
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_transcribe_creates_artifact_and_audits(monkeypatch):
    """Broker-routed voice.transcribe creates a transcript artifact for the correct org."""
    org = _unique_org()
    agent = _make_agent(org_id=org)
    audited = _patch_broker_infra(monkeypatch)
    _force_voice_models(monkeypatch)
    _stub_stt(monkeypatch)

    # Save an audio artifact directly (no broker call → no rate hit).
    from core.artifacts import get_artifact, read_artifact_content, save_artifact

    audio_artifact_id = await save_artifact(
        _FAKE_AUDIO,
        kind="attachment",
        title="test_audio.mp3",
        org_id=org,
        mime_type="audio/mpeg",
        created_by="test",
    )

    from core.tool_broker import tool_broker

    result = await tool_broker.execute(
        agent, "voice.transcribe", {"artifact_id": audio_artifact_id}
    )

    # --- ToolResult assertions ---
    assert result.data["status"] == "success", f"unexpected: {result.data}"
    assert result.data["transcript"] == _FAKE_TRANSCRIPT
    transcript_art_id = result.data["transcript_artifact_id"]
    assert isinstance(transcript_art_id, str) and transcript_art_id

    # --- Artifact assertions (real DB persistence round-trip) ---
    meta = await get_artifact(transcript_art_id)
    assert meta is not None
    assert meta["kind"] == "transcript"
    assert str(meta["organization_id"]) == org
    assert meta["mime_type"] == "text/plain"

    content = await read_artifact_content(transcript_art_id)
    assert content is not None
    assert content.decode("utf-8") == _FAKE_TRANSCRIPT

    # --- Title carries transcript preview ---
    assert "Transcript:" in (meta["title"] or "")

    # --- Audit trail ---
    assert "tool_call" in audited
    assert "tool_result" in audited


# ---------------------------------------------------------------------------
# Test 2: TTS success — artifact created, audited, readable, metadata in title
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_speak_creates_artifact_and_audits(monkeypatch):
    """Broker-routed voice.speak creates an audio artifact for the correct org."""
    org = _unique_org()
    agent = _make_agent(org_id=org)
    audited = _patch_broker_infra(monkeypatch)
    _force_voice_models(monkeypatch)
    _stub_tts(monkeypatch)

    from core.tool_broker import tool_broker

    result = await tool_broker.execute(
        agent, "voice.speak", {"text": "Hello world", "voice": "echo"}
    )

    # --- ToolResult assertions ---
    assert result.data["status"] == "success", f"unexpected: {result.data}"
    audio_art_id = result.data["audio_artifact_id"]
    assert isinstance(audio_art_id, str) and audio_art_id

    tts_meta = result.data["tts_meta"]
    assert tts_meta["voice"] == "echo"
    assert tts_meta["model"] == "test/tts-stub"
    assert "Hello world" in tts_meta["text_preview"]

    # --- Artifact assertions (real DB persistence round-trip) ---
    from core.artifacts import get_artifact, read_artifact_content

    meta = await get_artifact(audio_art_id)
    assert meta is not None
    assert meta["kind"] == "audio"
    assert str(meta["organization_id"]) == org
    assert meta["mime_type"] == "audio/mpeg"

    content = await read_artifact_content(audio_art_id)
    assert content == _FAKE_AUDIO

    # --- TTS metadata baked into title (model, voice, text preview) ---
    title = meta["title"] or ""
    assert "echo" in title
    assert "test/tts-stub" in title
    assert "Hello world" in title

    # --- Audit trail ---
    assert "tool_call" in audited
    assert "tool_result" in audited


# ---------------------------------------------------------------------------
# Test 3: Honest degraded — STT empty model → unavailable, no artifact, audited
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_transcribe_degraded_no_provider(monkeypatch):
    """With no STT provider configured, result is 'unavailable'; no artifact created."""
    import connectors.voice as vc
    from core.config import Settings

    org = _unique_org()
    agent = _make_agent(org_id=org)
    audited = _patch_broker_infra(monkeypatch)

    # Explicitly empty stt_model.
    monkeypatch.setattr(vc, "settings", Settings(stt_model="", tts_model=""))

    from core.artifacts import save_artifact

    audio_artifact_id = await save_artifact(
        _FAKE_AUDIO,
        kind="attachment",
        title="test_audio.mp3",
        org_id=org,
        mime_type="audio/mpeg",
        created_by="test",
    )

    from core.tool_broker import tool_broker

    result = await tool_broker.execute(
        agent, "voice.transcribe", {"artifact_id": audio_artifact_id}
    )

    assert result.data["status"] == "unavailable"
    assert "fallback_reason" in result.data
    assert "transcript" not in result.data
    assert "transcript_artifact_id" not in result.data

    # Still audited.
    assert "tool_call" in audited
    assert "tool_result" in audited


# ---------------------------------------------------------------------------
# Test 4: Honest degraded — TTS empty model → unavailable, no artifact, audited
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_speak_degraded_no_provider(monkeypatch):
    """With no TTS provider configured, result is 'unavailable'; no artifact created."""
    import connectors.voice as vc
    from core.config import Settings

    org = _unique_org()
    agent = _make_agent(org_id=org)
    audited = _patch_broker_infra(monkeypatch)

    monkeypatch.setattr(vc, "settings", Settings(stt_model="", tts_model=""))

    from core.tool_broker import tool_broker

    result = await tool_broker.execute(
        agent, "voice.speak", {"text": "test text"}
    )

    assert result.data["status"] == "unavailable"
    assert "fallback_reason" in result.data
    assert "audio_artifact_id" not in result.data

    assert "tool_call" in audited
    assert "tool_result" in audited


# ---------------------------------------------------------------------------
# Test 5: STT provider error → honest error ToolResult, no artifact, no raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_transcribe_provider_error_degrades(monkeypatch):
    """STT provider failure → honest error ToolResult; must not raise out of broker."""
    import connectors.voice as vc

    org = _unique_org()
    agent = _make_agent(org_id=org)
    _patch_broker_infra(monkeypatch)
    _force_voice_models(monkeypatch)

    async def boom(audio_bytes: bytes, mime: str) -> str:
        raise RuntimeError("STT provider exploded")

    monkeypatch.setattr(vc, "_call_stt", boom)

    from core.artifacts import save_artifact

    audio_artifact_id = await save_artifact(
        _FAKE_AUDIO,
        kind="attachment",
        title="test_audio.mp3",
        org_id=org,
        mime_type="audio/mpeg",
        created_by="test",
    )

    from core.tool_broker import tool_broker

    result = await tool_broker.execute(
        agent, "voice.transcribe", {"artifact_id": audio_artifact_id}
    )

    assert result.data["status"] == "error"
    assert "transcript_artifact_id" not in result.data


# ---------------------------------------------------------------------------
# Test 6: TTS provider error → honest error ToolResult, no artifact, no raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_speak_provider_error_degrades(monkeypatch):
    """TTS provider failure → honest error ToolResult; must not raise out of broker."""
    import connectors.voice as vc

    org = _unique_org()
    agent = _make_agent(org_id=org)
    _patch_broker_infra(monkeypatch)
    _force_voice_models(monkeypatch)

    async def boom(text: str, voice: str) -> bytes:
        raise RuntimeError("TTS provider exploded")

    monkeypatch.setattr(vc, "_call_tts", boom)

    from core.tool_broker import tool_broker

    result = await tool_broker.execute(
        agent, "voice.speak", {"text": "hello"}
    )

    assert result.data["status"] == "error"
    assert "audio_artifact_id" not in result.data


# ---------------------------------------------------------------------------
# Test 7: Cross-org — org-B agent cannot transcribe org-A audio artifact
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_transcribe_cross_org_rejected(monkeypatch):
    """Org-B agent transcribing org-A audio → rejected (honest error, no transcript)."""
    org_a = _unique_org()
    org_b = _unique_org()
    agent_b = _make_agent(org_id=org_b)

    _patch_broker_infra(monkeypatch)
    _force_voice_models(monkeypatch)
    _stub_stt(monkeypatch)

    # Save audio artifact belonging to org A directly.
    from core.artifacts import save_artifact

    audio_artifact_id = await save_artifact(
        _FAKE_AUDIO,
        kind="attachment",
        title="org_a_audio.mp3",
        org_id=org_a,
        mime_type="audio/mpeg",
        created_by="test",
    )

    # Org B agent attempts to transcribe org A's audio.
    from core.tool_broker import tool_broker

    result = await tool_broker.execute(
        agent_b, "voice.transcribe", {"artifact_id": audio_artifact_id}
    )

    # Connector must reject with an honest error — no transcript.
    assert result.data["status"] == "error"
    assert "transcript" not in result.data
    assert "transcript_artifact_id" not in result.data

    # Confirm no transcript artifact was created for either org.
    from core.db import engine, reflect_table
    from sqlalchemy import select

    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(artifacts.c.id).where(
                    artifacts.c.kind == "transcript",
                    artifacts.c.organization_id.in_([org_a, org_b]),
                )
            )
        ).fetchall()
    assert len(rows) == 0, f"unexpected transcript artifacts: {rows}"


# ---------------------------------------------------------------------------
# Test 8: Tool registry — voice tools in all three registry sets
# ---------------------------------------------------------------------------

def test_voice_tools_in_all_tool_sets():
    from runtime.tool_registry import ALL_TOOLS, SUBAGENT_TOOLS, INLINE_CHAT_TOOLS, tool_name

    all_names = {tool_name(t) for t in ALL_TOOLS}
    sub_names = {tool_name(t) for t in SUBAGENT_TOOLS}
    inline_names = {tool_name(t) for t in INLINE_CHAT_TOOLS}

    assert "voice__transcribe" in all_names
    assert "voice__speak" in all_names
    assert "voice__transcribe" in sub_names
    assert "voice__speak" in sub_names
    assert "voice__transcribe" in inline_names
    assert "voice__speak" in inline_names


# ---------------------------------------------------------------------------
# Test 9: Broker name conversion
# ---------------------------------------------------------------------------

def test_voice_broker_name_conversion():
    from runtime.tool_registry import to_broker_name

    assert to_broker_name("voice__transcribe") == "voice.transcribe"
    assert to_broker_name("voice__speak") == "voice.speak"


@pytest.mark.asyncio
async def test_voice_transcribe_endpoint_rejects_cross_org_conversation(monkeypatch):
    """The /chat/voice/transcribe endpoint must not link a transcript to a
    conversation owned by a different org (tenant isolation)."""
    import uuid
    from fastapi import HTTPException
    from sqlalchemy import insert
    from core.db import engine, reflect_table
    from core.models import Member
    import routers.chat as chat_router

    org_a = _unique_org()
    org_b = _unique_org()

    # A conversation owned by org A.
    conversations = await reflect_table("conversations")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(conversations)
                .values(
                    organization_id=org_a,
                    region="us",
                    member_id=str(uuid.uuid4()),
                    title="org A private convo",
                )
                .returning(conversations.c.id)
            )
        ).first()
    convo_id = str(row[0])

    member_b = Member(id=str(uuid.uuid4()), organization_id=org_b, email="b@example.com", role="user")
    req = chat_router.VoiceTranscribeRequest(artifact_id=str(uuid.uuid4()), conversation_id=convo_id)

    with pytest.raises(HTTPException) as exc:
        await chat_router.voice_transcribe(req, member=member_b)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_voice_transcribe_inline_audio_b64(monkeypatch):
    """The inline audio_b64 path transcribes without an artifact and persists a transcript."""
    import base64
    org = _unique_org()
    agent = _make_agent(org_id=org)
    _patch_broker_infra(monkeypatch)
    _force_voice_models(monkeypatch)
    _stub_stt(monkeypatch, transcript="inline transcript")

    from core.tool_broker import tool_broker
    from core.artifacts import get_artifact
    audio_b64 = base64.b64encode(_FAKE_AUDIO).decode()
    result = await tool_broker.execute(agent, "voice.transcribe", {"audio_b64": audio_b64})
    assert result.data["status"] == "success"
    assert result.data["transcript"] == "inline transcript"
    tid = result.data["transcript_artifact_id"]
    meta = await get_artifact(tid)
    assert meta is not None and meta.get("kind") == "transcript"


@pytest.mark.asyncio
async def test_voice_transcribe_bad_audio_b64_honest_error(monkeypatch):
    """Malformed audio_b64 returns an honest error, not a crash."""
    org = _unique_org()
    agent = _make_agent(org_id=org)
    _patch_broker_infra(monkeypatch)
    _force_voice_models(monkeypatch)
    _stub_stt(monkeypatch)

    from core.tool_broker import tool_broker
    result = await tool_broker.execute(agent, "voice.transcribe", {"audio_b64": "not_base64_$$$"})
    assert result.data["status"] == "error"
    assert "base64" in result.data.get("reason", "").lower()
