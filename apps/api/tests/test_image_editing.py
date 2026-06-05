"""Acceptance proof for Task 5: Image editing.

Tests verify:
- image.edit routes through the tool broker (audit trail asserted).
- With a stubbed _call_edit_provider, a NEW VERSION of the source artifact is
  created; the original bytes are preserved at the prior version (non-destructive).
- Honest degraded: settings.image_model="" → status "unavailable", NO new version
  created, call is still audited.
- Provider error: _call_edit_provider raises → honest error ToolResult, no new
  version, error does not propagate.
- Cross-org: an agent for org B calling image.edit on org A's artifact is
  rejected with an honest error ToolResult; no new version created.
- No regressions against test_image_generation.py, test_artifact_workspace.py,
  test_inline_tools.py.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from core.models import AgentContext


# ---------------------------------------------------------------------------
# Helpers (reuse patterns from test_image_generation.py)
# ---------------------------------------------------------------------------

_ORG_COUNTER = iter(range(3000, 5000))


def _unique_org() -> str:
    return f"edit-org-{next(_ORG_COUNTER)}"


def _make_agent(org_id: str = "default") -> AgentContext:
    import uuid
    return AgentContext(
        id=str(uuid.uuid4()),
        org_id=org_id,
        task_id=str(uuid.uuid4()),
        member_id=str(uuid.uuid4()),
    )


# Minimal valid PNG bytes (1×1 white pixel) — source image.
_FAKE_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

# Distinguishably different bytes for the edited result.
_EDITED_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\x00\x00\x01\x08\x00\x05\x18\xd9N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _patch_broker_infra(monkeypatch):
    """Patch broker's audit, permissions, tool_policy, connector_tier.

    Returns list of audited event types so tests can assert audit presence.
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


def _stub_edit_provider(monkeypatch, blobs: list[bytes]) -> None:
    """Monkeypatch the connector's _call_edit_provider to return deterministic bytes."""
    import connectors.image_gen as ig

    async def fake_edit(image_bytes, prompt, mask=None, operation="edit"):
        return list(blobs)

    monkeypatch.setattr(ig, "_call_edit_provider", fake_edit)


def _force_image_model(monkeypatch, model: str = "test/dall-e-stub") -> None:
    """Override settings.image_model in the connector module."""
    import connectors.image_gen as ig
    from core.config import Settings

    fake_settings = Settings(image_model=model)
    monkeypatch.setattr(ig, "settings", fake_settings)


async def _make_source_artifact(org_id: str) -> str:
    """Create a source image artifact in org_id; return its artifact_id."""
    from core.artifacts import save_artifact
    return await save_artifact(
        _FAKE_PNG,
        kind="image",
        title="Source image for editing tests",
        org_id=org_id,
        mime_type="image/png",
        created_by="test_setup",
    )


# ---------------------------------------------------------------------------
# Test 1: Success — new version created, original preserved, audited
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_edit_creates_new_version_and_preserves_source(monkeypatch):
    """image.edit writes a new artifact version; original bytes remain at prior version."""
    org = _unique_org()
    agent = _make_agent(org_id=org)
    audited = _patch_broker_infra(monkeypatch)
    _force_image_model(monkeypatch)
    _stub_edit_provider(monkeypatch, [_EDITED_PNG])

    # Create the source artifact directly (no need for generate round-trip).
    source_id = await _make_source_artifact(org)

    from core.artifacts import get_artifact, read_artifact_content
    from core.artifact_versions import list_versions, read_version_content

    # Capture state before the edit.
    pre_head = await get_artifact(source_id)
    pre_version: int = int(pre_head["version"])
    pre_versions = await list_versions(source_id, org)
    original_bytes = await read_artifact_content(source_id)

    from core.tool_broker import tool_broker
    result = await tool_broker.execute(
        agent, "image.edit", {"artifact_id": source_id, "prompt": "make it blue"}
    )

    # --- ToolResult assertions ---
    assert result.data["status"] == "success", f"unexpected status: {result.data}"
    assert result.data["artifact_id"] == source_id
    new_version: int = int(result.data["version"])
    assert new_version == pre_version + 1

    # --- Version count increased by 1 ---
    post_versions = await list_versions(source_id, org)
    assert len(post_versions) == len(pre_versions) + 1

    # --- HEAD content is the edited bytes ---
    head_bytes = await read_artifact_content(source_id)
    assert head_bytes == _EDITED_PNG

    # --- Original (prior version) bytes are still readable — NON-DESTRUCTIVE ---
    prior_bytes = await read_version_content(source_id, pre_version, org)
    assert prior_bytes == original_bytes == _FAKE_PNG

    # --- Edit params present in result ---
    edit_meta = result.data["edit_meta"]
    assert edit_meta["prompt"] == "make it blue"
    assert edit_meta["operation"] == "edit"
    assert edit_meta["model"] == "test/dall-e-stub"

    # --- Broker audited the call ---
    assert "tool_call" in audited
    assert "tool_result" in audited


# ---------------------------------------------------------------------------
# Test 2: Honest degraded — no provider → unavailable, no version created
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_edit_degraded_no_provider(monkeypatch):
    """With no image provider configured, result is 'unavailable'; no new version created."""
    import connectors.image_gen as ig
    from core.config import Settings

    org = _unique_org()
    agent = _make_agent(org_id=org)
    audited = _patch_broker_infra(monkeypatch)

    # Explicitly empty image_model.
    monkeypatch.setattr(ig, "settings", Settings(image_model=""))

    source_id = await _make_source_artifact(org)

    from core.artifact_versions import list_versions
    pre_versions = await list_versions(source_id, org)

    from core.tool_broker import tool_broker
    result = await tool_broker.execute(
        agent, "image.edit", {"artifact_id": source_id, "prompt": "test degraded"}
    )

    assert result.data["status"] == "unavailable"
    assert "fallback_reason" in result.data

    # No new version was created.
    post_versions = await list_versions(source_id, org)
    assert len(post_versions) == len(pre_versions)

    # Call is still audited even in the degraded path.
    assert "tool_call" in audited
    assert "tool_result" in audited


# ---------------------------------------------------------------------------
# Test 3: Provider error — honest error ToolResult, no new version, no propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_edit_provider_error_degrades(monkeypatch):
    """A provider error must return an honest error ToolResult, never propagate."""
    import connectors.image_gen as ig

    org = _unique_org()
    agent = _make_agent(org_id=org)
    _patch_broker_infra(monkeypatch)
    _force_image_model(monkeypatch)

    async def boom(image_bytes, prompt, mask=None, operation="edit"):
        raise RuntimeError("edit provider exploded")

    monkeypatch.setattr(ig, "_call_edit_provider", boom)

    source_id = await _make_source_artifact(org)

    from core.artifact_versions import list_versions
    pre_versions = await list_versions(source_id, org)

    from core.tool_broker import tool_broker
    result = await tool_broker.execute(
        agent, "image.edit", {"artifact_id": source_id, "prompt": "x"}
    )

    assert result.data["status"] == "error"
    assert "provider call failed" in result.data.get("reason", "")

    # No new version was created despite the provider failure.
    post_versions = await list_versions(source_id, org)
    assert len(post_versions) == len(pre_versions)


# ---------------------------------------------------------------------------
# Test 4: Cross-org — org B agent rejected from org A's artifact
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_edit_cross_org_rejected(monkeypatch):
    """An agent for org B must be rejected when trying to edit org A's artifact."""
    org_a = _unique_org()
    org_b = _unique_org()
    agent_b = _make_agent(org_id=org_b)

    _patch_broker_infra(monkeypatch)
    _force_image_model(monkeypatch)
    _stub_edit_provider(monkeypatch, [_EDITED_PNG])

    # Artifact belongs to org A.
    source_id = await _make_source_artifact(org_a)

    from core.artifact_versions import list_versions
    pre_versions = await list_versions(source_id, org_a)

    from core.tool_broker import tool_broker
    result = await tool_broker.execute(
        agent_b, "image.edit", {"artifact_id": source_id, "prompt": "steal this image"}
    )

    assert result.data["status"] == "error"
    assert "not found" in result.data.get("reason", "").lower()

    # Org A's artifact has no new version.
    post_versions = await list_versions(source_id, org_a)
    assert len(post_versions) == len(pre_versions)


# ---------------------------------------------------------------------------
# Test 5: Validation — missing artifact_id or prompt returns honest error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_edit_missing_args_returns_error(monkeypatch):
    """Missing required args (artifact_id or prompt) produce honest error ToolResults."""
    org = _unique_org()
    agent = _make_agent(org_id=org)
    _patch_broker_infra(monkeypatch)
    _force_image_model(monkeypatch)

    from core.tool_broker import tool_broker

    # Missing artifact_id.
    r = await tool_broker.execute(agent, "image.edit", {"prompt": "something"})
    assert r.data["status"] == "error"
    assert "artifact_id" in r.data.get("reason", "")

    # Missing prompt.
    source_id = await _make_source_artifact(org)
    r2 = await tool_broker.execute(agent, "image.edit", {"artifact_id": source_id})
    assert r2.data["status"] == "error"
    assert "prompt" in r2.data.get("reason", "")


# ---------------------------------------------------------------------------
# Test 6: Tool registry — IMAGE_EDIT present in all three lists
# ---------------------------------------------------------------------------

def test_image_edit_in_all_tool_sets():
    from runtime.tool_registry import ALL_TOOLS, SUBAGENT_TOOLS, INLINE_CHAT_TOOLS, tool_name

    all_names = {tool_name(t) for t in ALL_TOOLS}
    sub_names = {tool_name(t) for t in SUBAGENT_TOOLS}
    inline_names = {tool_name(t) for t in INLINE_CHAT_TOOLS}

    assert "image__edit" in all_names
    assert "image__edit" in sub_names
    assert "image__edit" in inline_names


# ---------------------------------------------------------------------------
# Test 7: broker_name conversion for image.edit
# ---------------------------------------------------------------------------

def test_image_edit_broker_name_conversion():
    from runtime.tool_registry import to_broker_name
    assert to_broker_name("image__edit") == "image.edit"
