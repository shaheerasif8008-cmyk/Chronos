"""Tests for full_auto autonomy gating in tool_broker.

Covers:
1. Hard floor (website.publish) raises ApprovalRequired even under full_auto.
2. A provider marked approval_required in settings is blocked under 'supervised'.
3. The same provider passes under 'full_auto' (settings-policy gate collapses).
4. Safety limits stay absolute regardless of autonomy.
"""
from __future__ import annotations

import types

import pytest


def _make_stub_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _drop_cached_tool_broker() -> None:
    import sys

    if "core.tool_broker" in sys.modules:
        del sys.modules["core.tool_broker"]
    core_mod = sys.modules.get("core")
    if core_mod is not None and hasattr(core_mod, "tool_broker"):
        delattr(core_mod, "tool_broker")


@pytest.fixture
def broker_env(monkeypatch):
    """Patch broker deps. Returns a knobs object to tune policy/autonomy/connector."""
    import sys

    knobs = types.SimpleNamespace(
        autonomy="supervised",
        policy={"enabled": True, "approval_required": False},
        connector_result=None,
    )

    cfg = types.SimpleNamespace(demo_mode=False, per_org_daily_token_limit=0, fast_model="stub", region="us")
    monkeypatch.setitem(sys.modules, "core.config", _make_stub_module("core.config", settings=cfg))

    class _FakeRedis:
        async def incr(self, k): return 1
        async def expire(self, k, t): pass
        async def get(self, k): return None
        async def set(self, *a, **kw): pass
        async def incrby(self, k, v): return v

    monkeypatch.setitem(sys.modules, "core.redis", _make_stub_module("core.redis", redis_client=_FakeRedis()))

    async def _noop(*a, **kw): pass
    monkeypatch.setitem(sys.modules, "core.audit", _make_stub_module("core.audit", log=_noop))

    async def _allow(*a, **kw): return True
    monkeypatch.setitem(sys.modules, "core.permissions", _make_stub_module("core.permissions", check=_allow))

    async def _tier(provider): return "fixture"
    async def _degraded_note(provider): return None
    monkeypatch.setitem(sys.modules, "core.connector_health", _make_stub_module(
        "core.connector_health", connector_tier=_tier, degraded_note=_degraded_note, check_connectors=_noop))

    async def _policy(*a, **kw): return dict(knobs.policy)
    async def _autonomy(*a, **kw): return knobs.autonomy
    async def _tool_permissions(*a, **kw): return dict(getattr(knobs, "tool_permissions", {}))
    monkeypatch.setitem(sys.modules, "core.settings_store", _make_stub_module(
        "core.settings_store", tool_policy=_policy, workspace_autonomy=_autonomy,
        tool_permissions=_tool_permissions))

    monkeypatch.setitem(sys.modules, "core.untrusted_content", _make_stub_module(
        "core.untrusted_content", scan_untrusted_content=lambda *a, **kw: {}))

    class ApprovalRequired(Exception):
        def __init__(self, tool="", reason=""): super().__init__(reason); self.tool = tool
    class ConnectorNotFound(Exception): pass
    class LoopDetected(Exception): pass
    class RateLimitExceeded(Exception): pass
    class SafetyLimitViolation(Exception): pass

    monkeypatch.setitem(sys.modules, "core.exceptions", _make_stub_module(
        "core.exceptions", ApprovalRequired=ApprovalRequired, ConnectorNotFound=ConnectorNotFound,
        LoopDetected=LoopDetected,
        RateLimitExceeded=RateLimitExceeded, SafetyLimitViolation=SafetyLimitViolation))

    class Member:
        def __init__(self, org_id="default"):
            self.id = "member-1"; self.organization_id = org_id; self.role = "user"

    class AgentContext:
        def __init__(self, org_id="default"):
            self.id = "agent-1"; self.org_id = org_id; self.task_id = None; self.workspace_id = "ws-1"
        def as_member(self): return Member(self.org_id)

    class ToolResult:
        def __init__(self, summary="", data=None):
            self.summary = summary; self.data = data or {}

    monkeypatch.setitem(sys.modules, "core.models", _make_stub_module(
        "core.models", AgentContext=AgentContext, ToolResult=ToolResult, Member=Member))

    # Stub the data connector that data.* tools route to.
    async def _data_execute(tool, args):
        return knobs.connector_result or ToolResult(summary="data ok", data={})
    monkeypatch.setitem(sys.modules, "connectors.data_analysis", _make_stub_module(
        "connectors.data_analysis", data_analysis_connector=types.SimpleNamespace(execute=_data_execute)))

    _drop_cached_tool_broker()

    yield knobs

    _drop_cached_tool_broker()


def _agent():
    from core.models import AgentContext
    return AgentContext()


@pytest.mark.asyncio
async def test_hard_floor_blocks_even_under_full_auto(broker_env):
    broker_env.autonomy = "full_auto"
    import core.tool_broker as tb
    from core.exceptions import ApprovalRequired
    with pytest.raises(ApprovalRequired):
        await tb.execute(_agent(), "website.publish", {"url": "https://x.com"})


@pytest.mark.asyncio
async def test_supervised_blocks_policy_approval_tool(broker_env):
    broker_env.autonomy = "supervised"
    broker_env.policy = {"enabled": True, "approval_required": True}
    import core.tool_broker as tb
    from core.exceptions import ApprovalRequired
    with pytest.raises(ApprovalRequired):
        await tb.execute(_agent(), "data.query", {"q": "select 1"})


@pytest.mark.asyncio
async def test_full_auto_passes_policy_approval_tool(broker_env):
    broker_env.autonomy = "full_auto"
    broker_env.policy = {"enabled": True, "approval_required": True}
    import core.tool_broker as tb
    result = await tb.execute(_agent(), "data.query", {"q": "select 1"})
    assert result.summary == "data ok"


@pytest.mark.asyncio
async def test_safety_limit_absolute_under_full_auto(broker_env):
    broker_env.autonomy = "full_auto"
    import core.tool_broker as tb
    from core.exceptions import SafetyLimitViolation
    with pytest.raises(SafetyLimitViolation):
        await tb.execute(_agent(), "computer.exec", {"command": "rm -rf /"})
