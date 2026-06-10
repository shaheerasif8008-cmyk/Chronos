"""Tests for skill.run_script routing in tool_broker.

Covers:
1. Happy path: valid skill_id + script_name executes successfully.
2. Path traversal guard: '../../../etc/passwd' raises ValueError.
3. Missing script: non-existent script raises FileNotFoundError.
"""
from __future__ import annotations

import json
import os
import types
import unittest.mock as mock
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Minimal stubs so the broker module can be imported without Postgres/Redis.
# ---------------------------------------------------------------------------


def _make_stub_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


@pytest.fixture(autouse=True)
def _patch_broker_deps(tmp_path, monkeypatch):
    """Patch all heavy dependencies so broker imports cleanly in unit tests."""
    import sys

    # --- core.config ---
    cfg = types.SimpleNamespace(
        demo_mode=False,
        per_org_daily_token_limit=0,
        fast_model="stub",
    )
    monkeypatch.setitem(sys.modules, "core.config", _make_stub_module("core.config", settings=cfg))

    # --- core.redis ---
    class _FakeRedis:
        async def incr(self, k): return 1
        async def expire(self, k, t): pass
        async def get(self, k): return None
        async def set(self, *a, **kw): pass
        async def incrby(self, k, v): return v

    monkeypatch.setitem(sys.modules, "core.redis", _make_stub_module("core.redis", redis_client=_FakeRedis()))

    # --- core.audit ---
    async def _noop(*a, **kw): pass
    monkeypatch.setitem(sys.modules, "core.audit", _make_stub_module("core.audit", log=_noop))

    # --- core.permissions ---
    async def _allow(*a, **kw): return True
    monkeypatch.setitem(sys.modules, "core.permissions", _make_stub_module("core.permissions", check=_allow))

    # --- core.connector_health ---
    async def _tier(provider): return "fixture"
    monkeypatch.setitem(sys.modules, "core.connector_health", _make_stub_module(
        "core.connector_health", connector_tier=_tier, check_connectors=_noop
    ))

    # --- core.settings_store ---
    async def _policy(*a, **kw): return {}
    monkeypatch.setitem(sys.modules, "core.settings_store", _make_stub_module("core.settings_store", tool_policy=_policy))

    # --- core.untrusted_content ---
    monkeypatch.setitem(sys.modules, "core.untrusted_content", _make_stub_module(
        "core.untrusted_content", scan_untrusted_content=lambda *a, **kw: {}
    ))

    # --- core.exceptions ---
    class ApprovalRequired(Exception):
        pass
    class LoopDetected(Exception):
        pass
    class RateLimitExceeded(Exception):
        pass
    class SafetyLimitViolation(Exception):
        pass

    monkeypatch.setitem(sys.modules, "core.exceptions", _make_stub_module(
        "core.exceptions",
        ApprovalRequired=ApprovalRequired,
        LoopDetected=LoopDetected,
        RateLimitExceeded=RateLimitExceeded,
        SafetyLimitViolation=SafetyLimitViolation,
    ))

    # --- core.models ---
    class Member:
        def __init__(self, org_id="default"):
            self.id = "member-1"
            self.organization_id = org_id
            self.role = "user"

    class AgentContext:
        def __init__(self, org_id="default", task_id=None):
            self.id = "agent-1"
            self.org_id = org_id
            self.task_id = task_id
            self.workspace_id = None

        def as_member(self):
            return Member(self.org_id)

    class ToolResult:
        def __init__(self, summary="", data=None):
            self.summary = summary
            self.data = data or {}

    monkeypatch.setitem(sys.modules, "core.models", _make_stub_module(
        "core.models",
        AgentContext=AgentContext,
        ToolResult=ToolResult,
        Member=Member,
    ))

    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(org_id="default"):
    from core.models import AgentContext
    return AgentContext(org_id=org_id)


def _make_skills_root(tmp_path: Path, skill_id: str, script_name: str, script_content: str) -> Path:
    """Create a fake skills directory with one script."""
    skill_dir = tmp_path / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / script_name).write_text(script_content)
    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_executes_script(tmp_path, monkeypatch):
    """skill.run_script resolves + executes a valid script."""
    import sys

    script_content = 'import json,sys; params=json.loads(sys.stdin.read() or "{}"); print(json.dumps({"ok": True, "leads": params.get("leads", [])}))'
    skills_root = _make_skills_root(tmp_path, "sdr-outreach", "icp-qualification.py", script_content)

    # Patch SKILLS_ROOT to point at tmp_path
    monkeypatch.setitem(
        sys.modules,
        "skills.registry",
        _make_stub_module("skills.registry", SKILLS_ROOT=skills_root),
    )

    from core.models import ToolResult

    # Stub data_analysis_connector to avoid spawning real subprocesses
    captured = {}

    async def _fake_execute(tool, args):
        captured["tool"] = tool
        captured["code"] = args.get("code", "")
        return ToolResult(summary="ok", data={"status": "success", "stdout_preview": '{"ok": true}'})

    fake_data_mod = _make_stub_module(
        "connectors.data_analysis",
        data_analysis_connector=types.SimpleNamespace(execute=_fake_execute),
    )
    monkeypatch.setitem(sys.modules, "connectors.data_analysis", fake_data_mod)

    # Import and call
    import importlib
    import core.tool_broker as tb_mod  # may already be cached
    importlib.invalidate_caches()

    # Re-import to pick up monkeypatched modules
    if "core.tool_broker" in sys.modules:
        del sys.modules["core.tool_broker"]
    import core.tool_broker as tb

    agent = _make_agent()
    result = await tb._route_skill_run_script(
        agent,
        {"skill_id": "sdr-outreach", "script_name": "icp-qualification.py", "params": {"leads": []}},
        "fixture",
    )

    assert captured["tool"] == "data.run_script"
    assert "icp-qualification.py" in captured["code"] or "import json" in captured["code"]
    assert result.summary == "ok"


@pytest.mark.asyncio
async def test_path_traversal_raises_value_error(tmp_path, monkeypatch):
    """Path traversal attempt raises ValueError before any I/O."""
    import sys

    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    (skills_root / "sdr-outreach").mkdir()

    monkeypatch.setitem(
        sys.modules,
        "skills.registry",
        _make_stub_module("skills.registry", SKILLS_ROOT=skills_root),
    )

    if "core.tool_broker" in sys.modules:
        del sys.modules["core.tool_broker"]
    import core.tool_broker as tb

    agent = _make_agent()
    with pytest.raises(ValueError, match="path traversal"):
        await tb._route_skill_run_script(
            agent,
            {
                "skill_id": "sdr-outreach",
                "script_name": "../../../etc/passwd",
                "params": {},
            },
            "fixture",
        )


@pytest.mark.asyncio
async def test_missing_script_raises_file_not_found(tmp_path, monkeypatch):
    """Non-existent script raises FileNotFoundError."""
    import sys

    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    (skills_root / "sdr-outreach").mkdir()

    monkeypatch.setitem(
        sys.modules,
        "skills.registry",
        _make_stub_module("skills.registry", SKILLS_ROOT=skills_root),
    )

    if "core.tool_broker" in sys.modules:
        del sys.modules["core.tool_broker"]
    import core.tool_broker as tb

    agent = _make_agent()
    with pytest.raises(FileNotFoundError, match="script not found"):
        await tb._route_skill_run_script(
            agent,
            {
                "skill_id": "sdr-outreach",
                "script_name": "does-not-exist.py",
                "params": {},
            },
            "fixture",
        )
