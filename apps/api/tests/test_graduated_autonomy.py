"""Unit tests for Graduated Autonomy: the Risk Pricer and the Autonomy Gate.

These exercise the decision logic directly (no DB). The trust ledger degrades to
cold-start defaults when no database is reachable, so get_trust_level returns the
seed standing here; graduation-from-earned-trust is tested by stubbing the level.
"""
from __future__ import annotations

import pytest

from core import autonomy, risk, trust
from core.risk import RiskScore
from core.trust import TrustLevel


# --- Risk Pricer ----------------------------------------------------------

def test_draft_is_lower_risk_than_external_send():
    draft = risk.price("gmail.draft", {"to": ["a@x.com"], "body": "hi"})
    send = risk.price("gmail.send", {"to": [f"u{i}@x.com" for i in range(8)]})
    assert draft.value < send.value
    assert draft.tier == "low"


def test_action_class_partitions_by_magnitude():
    single = risk.price("gmail.send", {"to": ["a@x.com"]})
    bulk = risk.price("gmail.send", {"to": ["a@x.com", "b@x.com"]})
    assert single.action_class == "gmail.send:single"
    assert bulk.action_class == "gmail.send:bulk"


def test_regulated_data_raises_risk():
    clean = risk.price("doc.create", {"title": "Notes"})
    pii = risk.price("doc.create", {"title": "SSN 123-45-6789"})
    assert pii.value > clean.value
    assert pii.factors["data_class"] == 1.0


def test_novelty_lowers_with_history():
    novel = risk.price("data.query", {"q": "x"}, novelty=1.0)
    known = risk.price("data.query", {"q": "x"}, novelty=0.0)
    assert novel.value > known.value


def test_tiers():
    assert risk.risk_tier(0.2) == "low"
    assert risk.risk_tier(0.5) == "medium"
    assert risk.risk_tier(0.9) == "high"


# --- Autonomy Gate --------------------------------------------------------

def _risk(value=0.5, action_class="data.query", tier=None):
    return RiskScore(value=value, action_class=action_class,
                     tier=tier or risk.risk_tier(value), factors={})


@pytest.mark.asyncio
async def test_full_auto_allows():
    d = await autonomy.evaluate("default", "ws", _risk(0.9), {}, {"approval_required": True}, "full_auto")
    assert d.allow


@pytest.mark.asyncio
async def test_lenient_policy_allows_under_supervised():
    d = await autonomy.evaluate("default", "ws", _risk(0.5), {}, {"approval_required": False}, "supervised")
    assert d.allow


@pytest.mark.asyncio
async def test_approval_required_blocks_when_not_graduated(monkeypatch):
    async def cold(*a, **k):
        return TrustLevel("data.query", 0.0, None, None, 0, 0, 0)
    monkeypatch.setattr(trust, "get_trust_level", cold)
    d = await autonomy.evaluate("default", "ws", _risk(0.5), {}, {"approval_required": True}, "supervised")
    assert not d.allow


@pytest.mark.asyncio
async def test_high_risk_never_graduates(monkeypatch):
    async def graduated(*a, **k):
        return TrustLevel("finance.transfer", 0.99, 0.9, "system", 99, 0, 0)
    monkeypatch.setattr(trust, "get_trust_level", graduated)
    d = await autonomy.evaluate("default", "ws", _risk(0.85), {}, {"approval_required": True}, "supervised")
    assert not d.allow


@pytest.mark.asyncio
async def test_low_risk_seeded_class_auto_executes():
    # gmail.draft is seeded -> cold-start auto_threshold; degrades to seed (no DB).
    r = risk.price("gmail.draft", {"to": ["a@x.com"], "body": "hi"})
    d = await autonomy.evaluate("default", "ws", r, {"to": ["a@x.com"]},
                                {"approval_required": True}, "supervised")
    assert d.allow


@pytest.mark.asyncio
async def test_medium_risk_needs_human_ratification(monkeypatch):
    async def seed_only(*a, **k):
        return TrustLevel("x", 0.9, 0.6, "seed", 30, 0, 0)
    monkeypatch.setattr(trust, "get_trust_level", seed_only)
    d = await autonomy.evaluate("default", "ws", _risk(0.5), {}, {"approval_required": True}, "supervised")
    assert not d.allow

    async def human(*a, **k):
        return TrustLevel("x", 0.9, 0.6, "member-7", 30, 0, 0)
    monkeypatch.setattr(trust, "get_trust_level", human)
    d2 = await autonomy.evaluate("default", "ws", _risk(0.5), {}, {"approval_required": True}, "supervised")
    assert d2.allow


@pytest.mark.asyncio
async def test_learned_policy_blocks(monkeypatch):
    async def match(*a, **k):
        return {"decision": "deny"}
    monkeypatch.setattr(autonomy, "_matching_learned_policy", match)
    d = await autonomy.evaluate("default", "ws", _risk(0.2, "gmail.draft"),
                                {"to": ["evil@competitor.com"]},
                                {"approval_required": False}, "supervised")
    assert not d.allow
    assert "learned policy" in d.reason


def test_seed_grants_threshold_to_seeded_class():
    level = trust._seed("gmail.draft")
    assert level.auto_threshold == 0.40
    assert level.graduated_by == "seed"
    unseeded = trust._seed("data.query")
    assert unseeded.auto_threshold is None
