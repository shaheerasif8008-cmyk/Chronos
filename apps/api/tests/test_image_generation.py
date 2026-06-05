"""Acceptance proof for Task 4: Image generation.

Tests verify:
- image.generate routes through the tool broker (audit trail asserted).
- With a stubbed provider, the generated image artifact is created (kind="image"),
  belongs to the correct org, and is re-readable via get_artifact /
  read_artifact_content after the call.  Generation metadata is present in
  ToolResult.data.
- Count cap: count above the broker safety limit raises SafetyLimitViolation.
- Honest degraded: settings.image_model="" → status "unavailable" with a
  fallback_reason; NO artifact created; call is still audited.
- Cross-org: an artifact saved for org A is NOT visible as org B's (caller-side
  org check, as get_artifact is id-only and callers enforce the boundary).
- No regressions in test_inline_tools.py or test_runtime_reliability_phase1.py.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from core.exceptions import SafetyLimitViolation
from core.models import AgentContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_COUNTER = iter(range(2000))


def _unique_org() -> str:
    return f"img-org-{next(_ORG_COUNTER)}"


def _make_agent(org_id: str = "default") -> AgentContext:
    import uuid
    return AgentContext(
        id=str(uuid.uuid4()),
        org_id=org_id,
        task_id=str(uuid.uuid4()),
        member_id=str(uuid.uuid4()),
    )


# Minimal valid PNG bytes (1×1 white pixel).
_FAKE_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _patch_broker_infra(monkeypatch):
    """Patch broker's audit, permissions, tool_policy, connector_tier.

    Returns list of audited event types so tests can assert audit presence.
    Does NOT patch rate limiting — tests use unique org ids to stay under the
    10-actions/minute/org cap.
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


def _stub_provider(monkeypatch, blobs: list[bytes]) -> None:
    """Monkeypatch the connector's _call_provider to return deterministic bytes."""
    import connectors.image_gen as ig

    async def fake_call_provider(prompt: str, size: str, count: int) -> list[bytes]:
        return blobs[:count]

    monkeypatch.setattr(ig, "_call_provider", fake_call_provider)


def _force_image_model(monkeypatch, model: str = "test/dall-e-stub") -> None:
    """Override settings.image_model in the connector module."""
    import connectors.image_gen as ig
    from core.config import Settings

    # Build a throwaway Settings instance so we can override a single field
    # without mutating the shared singleton (lru_cache).
    fake_settings = Settings(image_model=model)
    monkeypatch.setattr(ig, "settings", fake_settings)


# ---------------------------------------------------------------------------
# Test 1: Success path — artifact created, audited, readable, metadata present
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_generate_creates_artifact_and_audits(monkeypatch):
    """Broker-routed image.generate creates an image artifact for the correct org."""
    org = _unique_org()
    agent = _make_agent(org_id=org)
    audited = _patch_broker_infra(monkeypatch)
    _force_image_model(monkeypatch)
    _stub_provider(monkeypatch, [_FAKE_PNG])

    from core.tool_broker import tool_broker
    result = await tool_broker.execute(agent, "image.generate", {"prompt": "a red square", "count": 1})

    # --- ToolResult assertions ---
    assert result.data["status"] == "success"
    artifact_ids = result.data["artifact_ids"]
    assert isinstance(artifact_ids, list) and len(artifact_ids) == 1

    # --- Artifact assertions ---
    from core.artifacts import get_artifact, read_artifact_content
    meta = await get_artifact(artifact_ids[0])
    assert meta is not None
    assert meta["kind"] == "image"
    assert str(meta["organization_id"]) == org
    assert meta["mime_type"] == "image/png"

    content = await read_artifact_content(artifact_ids[0])
    assert content == _FAKE_PNG

    # --- Generation metadata in ToolResult ---
    gen_meta = result.data["generation_meta"]
    assert gen_meta["prompt"] == "a red square"
    assert gen_meta["model"] == "test/dall-e-stub"

    # --- Audit trail ---
    assert "tool_call" in audited
    assert "tool_result" in audited


# ---------------------------------------------------------------------------
# Test 2: Count cap — broker safety limit rejects count above 4
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_generate_count_cap_raises(monkeypatch):
    """Count > 4 raises SafetyLimitViolation before reaching the connector."""
    org = _unique_org()
    agent = _make_agent(org_id=org)
    _patch_broker_infra(monkeypatch)
    _force_image_model(monkeypatch)
    _stub_provider(monkeypatch, [_FAKE_PNG] * 5)

    from core.tool_broker import tool_broker
    with pytest.raises(SafetyLimitViolation):
        await tool_broker.execute(agent, "image.generate", {"prompt": "many images", "count": 5})


# ---------------------------------------------------------------------------
# Test 3: Honest degraded — no provider → unavailable result, no artifact, audited
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_generate_degraded_no_provider(monkeypatch):
    """With no image provider configured, result is 'unavailable'; no artifact is created."""
    import connectors.image_gen as ig
    from core.config import Settings

    org = _unique_org()
    agent = _make_agent(org_id=org)
    audited = _patch_broker_infra(monkeypatch)

    # Explicitly empty image_model (the default, but be explicit here).
    monkeypatch.setattr(ig, "settings", Settings(image_model=""))

    from core.tool_broker import tool_broker
    result = await tool_broker.execute(agent, "image.generate", {"prompt": "test degraded"})

    assert result.data["status"] == "unavailable"
    assert "fallback_reason" in result.data
    assert result.data["images"] == []
    # No artifact_ids in a degraded result.
    assert "artifact_ids" not in result.data

    # Call is still audited even in the degraded path.
    assert "tool_call" in audited
    assert "tool_result" in audited


# ---------------------------------------------------------------------------
# Test 4: Cross-org — org B agent cannot see org A's image artifact
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_generate_cross_org_isolation(monkeypatch):
    """Artifact saved for org A is not accessible as org B's artifact.

    get_artifact returns the row regardless of org (it's id-only); the caller
    enforces the tenant boundary by comparing meta['organization_id'] to the
    requester's org.  This test confirms the artifact is stored with the correct
    org and that a cross-org check (as performed by chat.py and other callers)
    returns the right org on the artifact so org B would be denied.
    """
    org_a = _unique_org()
    org_b = _unique_org()
    agent_a = _make_agent(org_id=org_a)

    _patch_broker_infra(monkeypatch)
    _force_image_model(monkeypatch)
    _stub_provider(monkeypatch, [_FAKE_PNG])

    from core.tool_broker import tool_broker
    result = await tool_broker.execute(agent_a, "image.generate", {"prompt": "org A secret image"})

    artifact_id = result.data["artifact_ids"][0]

    from core.artifacts import get_artifact
    meta = await get_artifact(artifact_id)
    assert meta is not None
    # The artifact belongs to org A — an org B caller would see this mismatch
    # and deny access (str comparison, matching chat.py pattern).
    assert str(meta["organization_id"]) == org_a
    assert str(meta["organization_id"]) != org_b


# ---------------------------------------------------------------------------
# Test 5: Tool registry — IMAGE_GENERATE present in all three lists
# ---------------------------------------------------------------------------

def test_image_generate_in_all_tool_sets():
    from runtime.tool_registry import ALL_TOOLS, SUBAGENT_TOOLS, INLINE_CHAT_TOOLS, tool_name

    all_names = {tool_name(t) for t in ALL_TOOLS}
    sub_names = {tool_name(t) for t in SUBAGENT_TOOLS}
    inline_names = {tool_name(t) for t in INLINE_CHAT_TOOLS}

    assert "image__generate" in all_names
    assert "image__generate" in sub_names
    assert "image__generate" in inline_names


# ---------------------------------------------------------------------------
# Test 6: broker_name conversion
# ---------------------------------------------------------------------------

def test_image_generate_broker_name_conversion():
    from runtime.tool_registry import to_broker_name
    assert to_broker_name("image__generate") == "image.generate"


# ---------------------------------------------------------------------------
# Test 7: Multiple images (count=2) — two artifacts created
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_generate_multiple_count(monkeypatch):
    """count=2 creates two artifacts."""
    org = _unique_org()
    agent = _make_agent(org_id=org)
    _patch_broker_infra(monkeypatch)
    _force_image_model(monkeypatch)
    _stub_provider(monkeypatch, [_FAKE_PNG, _FAKE_PNG])

    from core.tool_broker import tool_broker
    result = await tool_broker.execute(agent, "image.generate", {"prompt": "two images", "count": 2})

    assert result.data["status"] == "success"
    assert result.data["count"] == 2
    assert len(result.data["artifact_ids"]) == 2
    assert result.data["generation_meta"]["count"] == 2
